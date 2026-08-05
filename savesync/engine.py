"""Sync orchestrator: scan -> match -> negotiate -> execute -> complete.

Phase-gated so nothing destroys data until the UI opts in:
  - Phase 1: negotiate only, return the plan, never touch disk.
  - Phase 2: execute downloads (and uploads only when `allow_upload=True`),
    treats conflicts as skips.
  - Phase 3: full bidirectional with conflict policy + savestate slots."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .api import RomMClient, RomMError
from .config import SyncConfig
from .models import LocalSave, SyncOperation, SyncPlan
from .scanner import (
    clean_server_filename,
    match_local_to_rom,
    normalize_stem,
    scan_save_dirs,
)

CONFLICT_KEEP_LOCAL = "keep_local"
CONFLICT_TAKE_SERVER = "take_server"
CONFLICT_AUTO = "auto"
CONFLICT_SKIP = "skip"
DEFAULT_CONFLICT_POLICY = CONFLICT_SKIP  # safest default until UI offers choices


@dataclass
class SyncResult:
    uploaded: int = 0
    downloaded: int = 0
    conflicts: int = 0
    no_ops: int = 0
    failed: list[str] = field(default_factory=list)
    plan: SyncPlan | None = None


class SyncEngine:
    def __init__(self, client: RomMClient, cfg: SyncConfig, debug: bool = False):
        self.client = client
        self.cfg = cfg
        self.debug = debug
        self._scanned: list[LocalSave] = []
        self._rom_to_fs_slug: dict[int, str] = {}

    # ------------------------------------------------------------------ scan

    def scan_platform(self, root: Path, fs_slug: str) -> list[LocalSave]:
        """Discover local saves for one platform folder hierarchy."""
        return scan_save_dirs(root, self.cfg.save_dirs, fs_slug)

    # ----------------------------------------------------------------- match

    def match(self, local_saves: list[LocalSave],
              roms: list[dict]) -> tuple[list[LocalSave], int]:
        """Resolve rom_id from the platform's ROM list. Returns (saves, matched)."""
        matched = match_local_to_rom(local_saves, roms)
        return local_saves, matched

    # ------------------------------------------------------------ orchestrate

    def run(self, plan: SyncPlan, *, dry_run: bool = True,
            allow_upload: bool = False,
            policy: str = DEFAULT_CONFLICT_POLICY,
            on_op: Callable[[int, int], None] | None = None) -> SyncResult:
        """Execute a plan (or, in dry-run/Phase 1, just report counts).

        `policy` applies to every `conflict` operation:
          auto | keep_local | take_server | skip
        `on_op(done, total)` is called after each applied operation so UIs can
        render progress (total includes no_op rows).
        """
        result = SyncResult(plan=plan)
        if policy not in (CONFLICT_AUTO, CONFLICT_KEEP_LOCAL,
                          CONFLICT_TAKE_SERVER, CONFLICT_SKIP):
            raise ValueError(f"unknown conflict policy: {policy!r}")
        if dry_run:
            self._report(plan)
            return result

        total = max(1, len(plan.operations))
        for done, op in enumerate(plan.operations, start=1):
            try:
                self._apply(op, allow_upload=allow_upload, policy=policy,
                            result=result)
            except Exception as exc:  # keep going; collect failures
                result.failed.append(f"{op.file_name} ({op.action}): {exc}")
                if self.debug:
                    raise
            if on_op:
                on_op(done, total)
        # always finalize, even on partial failure
        self.client.complete_session(
            plan.session_id,
            result.uploaded + result.downloaded,
            len(result.failed),
        )
        return result

    # ----------------------------------------------------------------- apply

    def _apply(self, op: SyncOperation, *, allow_upload: bool,
               policy: str, result: SyncResult) -> None:
        if op.action == "no_op":
            result.no_ops += 1
        elif op.action == "download":
            dest = self._dest_path(op)
            self.client.download_save(op, dest, self.cfg.device_id)
            if op.save_id:
                self.client.confirm_download(op.save_id, self.cfg.device_id)
            result.downloaded += 1
        elif op.action == "upload":
            if not allow_upload:
                result.no_ops += 1
                return
            local = self._local_for(op)
            if not local.file_path or not Path(local.file_path).exists():
                raise RomMError(f"no local file for upload op {op.file_name}")
            self.client.upload_save(local, self.cfg.device_id, overwrite=False)
            result.uploaded += 1
        elif op.action == "conflict":
            result.conflicts += 1
            self._resolve_conflict(op, policy, allow_upload, result)

    def _resolve_conflict(self, op: SyncOperation, policy: str,
                          allow_upload: bool, result: SyncResult) -> None:
        """Apply the per-conflict policy. Returns without acting on SKIP.

        resolved_as (upload|download|skip) is stashed on the op so the UI can
        show what a given policy would do before committing.
        """
        if policy == CONFLICT_SKIP:
            op.resolved_as = "skip"
            return
        if policy == CONFLICT_KEEP_LOCAL:
            self._conflict_upload(op, allow_upload, result)
            return
        if policy == CONFLICT_TAKE_SERVER:
            self._conflict_download(op, result)
            return
        if policy == CONFLICT_AUTO:
            # newest updated_at wins; tie -> keep local (the file we control)
            local_ts = self._local_for(op).updated_at
            server_ts = op.server_updated_at
            if server_ts and local_ts and server_ts > local_ts:
                self._conflict_download(op, result)
            else:
                self._conflict_upload(op, allow_upload, result)
            return
        raise ValueError(f"unknown conflict policy: {policy!r}")

    def _conflict_upload(self, op: SyncOperation, allow_upload: bool,
                         result: SyncResult) -> None:
        local = self._local_for(op)
        if not allow_upload:
            op.resolved_as = "skip"
            return
        if not local.file_path or not Path(local.file_path).exists():
            raise RomMError(f"conflict keep-local: no local file {op.file_name}")
        self.client.upload_save(local, self.cfg.device_id, overwrite=True)
        result.uploaded += 1
        op.resolved_as = "upload"

    def _conflict_download(self, op: SyncOperation, result: SyncResult) -> None:
        dest = self._dest_path(op)
        self.client.download_save(op, dest, self.cfg.device_id)
        if op.save_id:
            self.client.confirm_download(op.save_id, self.cfg.device_id)
        result.downloaded += 1
        op.resolved_as = "download"

    # -------------------------------------------------------------- plumbing

    def _local_for(self, op: SyncOperation) -> LocalSave:
        """Rehydrate a LocalSave for upload from an operation.

        The op's `file_name` is the *server* name (carries a [timestamp] tag
        on conflicts), so match the scanned list by rom_id + slot with
        tag-normalized stems. Raises if no real local file backs the op.
        """
        for ls in self._scanned or []:
            if ls.rom_id != op.rom_id:
                continue
            if (ls.slot or "autosave") != (op.slot or "autosave"):
                continue
            if normalize_stem(ls.file_name) == normalize_stem(op.file_name):
                return ls
        # fall back: any local save for this rom_id + slot (single-file case)
        for ls in self._scanned or []:
            if ls.rom_id == op.rom_id and (ls.slot or "autosave") == (op.slot or "autosave"):
                if ls.file_path and Path(ls.file_path).exists():
                    return ls
        return LocalSave(rom_id=op.rom_id, file_name=op.file_name,
                         emulator=op.emulator, slot=op.slot)

    def set_scanned_saves(self, saves: list[LocalSave]) -> None:
        self._scanned = saves

    def set_platform_map(self, rom_to_fs_slug: dict[int, str]) -> None:
        """rom_id -> platform fs_slug, used to pick a save dir for downloads."""
        self._rom_to_fs_slug = rom_to_fs_slug

    def preview_conflicts(self, plan: SyncPlan, policy: str) -> list[tuple[SyncOperation, str]]:
        """Compute what each conflict would resolve to under `policy`.

        Pure decision logic  no network, no disk writes  so the UI can show
        keep-local/take-server/skip per game before the user commits. Returns
        (op, resolved_as) for conflict ops only.
        """
        out: list[tuple[SyncOperation, str]] = []
        for op in plan.operations:
            if op.action != "conflict":
                continue
            if policy == CONFLICT_KEEP_LOCAL:
                out.append((op, "upload"))
            elif policy == CONFLICT_TAKE_SERVER:
                out.append((op, "download"))
            elif policy == CONFLICT_AUTO:
                local_ts = self._local_for(op).updated_at
                server_ts = op.server_updated_at
                out.append((op, "download" if (server_ts and local_ts and server_ts > local_ts) else "upload"))
            else:
                out.append((op, "skip"))
        return out

    def _dest_path(self, op: SyncOperation) -> str:
        """Resolve the local destination for a download operation.

        Uses the op's rom_id to look up the platform fs_slug (via
        `set_platform_map`), picks the first configured save dir for that
        platform (or the \"*\" wildcard), and writes with the `[timestamp]`
        tag stripped from the server filename so the emulator sees a clean
        name (e.g. `Wario Land 3 (World) (En,Ja).srm`).
        """
        slug = (self._rom_to_fs_slug or {}).get(op.rom_id)
        candidates = self.cfg.save_dirs.get(slug, []) if slug else []
        if not candidates:
            candidates = self.cfg.save_dirs.get("*", [])
        if not candidates:
            raise ValueError(f"no save dir configured for platform {slug!r} "
                             f"(rom {op.rom_id}) in save_directories.json")
        dest_dir = Path(candidates[0])
        if not dest_dir.is_absolute():
            dest_dir = (self.cfg.save_root or Path.cwd()) / dest_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        return str(dest_dir / clean_server_filename(op.file_name))

    def _report(self, plan: SyncPlan) -> None:
        print(f"[savesync] plan: {plan.total_upload} up, {plan.total_download} down, "
              f"{plan.total_conflict} conflict, {plan.total_no_op} noop "
              f"(session {plan.session_id})")