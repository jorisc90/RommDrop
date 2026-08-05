"""Shared sync pipeline  used by both the CLI and the pygame GUI.

Centralizes scan -> match -> negotiate -> execute so the two front-ends
(terminal and GUI) drive the exact same, live-verified code path.

Entry points:
  scan_negotiate(client, cfg, root, *, platform=None, debug=False)
      read-only: returns (engine, plan) after scanning every platform in
      cfg.save_dirs, matching local saves to server ROMs, and negotiating.

  execute(engine, client, plan, *, allow_upload=False, policy=..., dry_run=False)
      applies the plan with a single conflict `policy` (auto | keep_local |
      take_server | skip), completing the session. Matches the CLI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .engine import DEFAULT_CONFLICT_POLICY, SyncEngine

if TYPE_CHECKING:
    from pathlib import Path

    from .api import RomMClient
    from .config import SyncConfig


def scan_negotiate(
    client: "RomMClient",
    cfg: "SyncConfig",
    root: "Path",
    *,
    platform: str | None = None,
    debug: bool = False,
):
    """Scan every configured platform, match to server ROMs, negotiate.

    Returns `(engine, plan)`. Read-only: no network pulls of file bytes and no
    disk writes. `platform` restricts the scan AND drops negotiated ops whose
    rom_id isn't mapped to that platform.
    """
    eng = SyncEngine(client, cfg, debug=debug)
    platforms = client.list_platforms()
    slug_to_id = {}
    for p in platforms:
        slug_to_id[p.get("fs_slug") or p.get("slug")] = p.get("id")
    if debug:
        print(f"[sync] platforms: {sorted(slug_to_id)}")

    rom_to_slug: dict[int, str] = {}
    scanned = []
    for slug in (cfg.save_dirs or {}).keys():
        if slug == "*" or slug.startswith("_"):
            continue
        if platform and slug != platform:
            continue
        pid = slug_to_id.get(slug)
        if not pid:
            if debug:
                print(f"[sync] no server platform for fs_slug {slug!r}; skipping")
            continue
        roms = client.list_roms(pid)
        for r in roms:
            rom_to_slug[int(r["id"])] = slug
        saves = eng.scan_platform(root, slug)
        if not saves:
            continue
        saves, matched = eng.match(saves, roms)
        scanned.extend(saves)

    eng.set_scanned_saves(scanned)
    eng.set_platform_map(rom_to_slug)
    plan = client.negotiate(cfg.device_id, scanned)
    mapped_rom_ids = set(rom_to_slug)
    if platform:
        # Drop ops for roms outside the requested scope (platform filter):
        # never execute something we can't resolve to a local dir.
        plan.operations = [o for o in plan.operations if o.rom_id in mapped_rom_ids]
    # totals derive from the ops we will actually execute, not the server's
    # reported numbers (which may include out-of-scope rows)
    _recount(plan)
    return eng, plan


def execute(
    engine: SyncEngine,
    client,
    plan,
    *,
    allow_upload: bool = False,
    policy: str = DEFAULT_CONFLICT_POLICY,
    dry_run: bool = False,
    on_op=None,
):
    """Execute a plan (download/upload/no-op), completing the session.

    `dry_run=True` reports counts only and completes 0/0 (mirror the CLI `plan`
    subcommand). `on_op(done, total)` is forwarded to the engine so UIs can
    render progress. Returns the SyncResult.
    """
    result = engine.run(plan, dry_run=dry_run,
                        allow_upload=allow_upload, policy=policy, on_op=on_op)
    if dry_run:
        # engine.run(dry_run=True) skips completion; avoid a dangling session
        client.complete_session(plan.session_id, 0, 0)
    return result


def _recount(plan) -> None:
    plan.total_upload = sum(o.action == "upload" for o in plan.operations)
    plan.total_download = sum(o.action == "download" for o in plan.operations)
    plan.total_conflict = sum(o.action == "conflict" for o in plan.operations)
    plan.total_no_op = sum(o.action == "no_op" for o in plan.operations)


class SyncSession:
    """Pygame-free GUI controller around the shared pipeline.

    Holds everything the SYNC screen needs (phase, plan, per-conflict policy
    preview, upload gate, result, progress) and drives scan_negotiate/execute.
    The GUI renders `phase`/`preview` and calls the methods below  the two
    entry points (scan, execute) are network+disk heavy and should run on a
    worker thread; the rest are cheap and event-loop safe.
    """

    PHASES = ("idle", "scanning", "ready", "running", "done", "error")

    def __init__(self, engine: SyncEngine, plan, client):
        self.engine = engine
        self.plan = plan
        self.client = client
        self.phase = "idle"
        self.status = ""
        self.error = ""
        self.policy = DEFAULT_CONFLICT_POLICY  # auto
        self.allow_upload = False
        self.result = None
        self.progress = (0, 0)
        # (op, resolved_as) preview per conflict, recomputed on policy change
        self._preview = []

    # -- phases -----------------------------------------------------------

    def scan(self) -> None:
        """Negotiate is done by scan_negotiate before the session exists, so
        this only recomputes the conflict preview for the current policy."""
        self.rebuild_preview()

    def rebuild_preview(self) -> None:
        self._preview = self.engine.preview_conflicts(self.plan, self.policy)

    def set_policy(self, policy: str) -> None:
        if policy in ("auto", "keep_local", "take_server", "skip"):
            self.policy = policy
        self.rebuild_preview()

    def toggle_upload(self) -> bool:
        self.allow_upload = not self.allow_upload
        return self.allow_upload

    # -- UI-facing read views ----------------------------------------------

    @property
    def preview_lines(self) -> list[tuple[str, str]]:
        """(file_name, resolved_as) for each conflict under the current policy."""
        return [
            (op.file_name, resolved)
            for op, resolved in self._preview
        ]

    @property
    def summary(self) -> str:
        p = self.plan
        return (
            f"{p.total_upload} up / {p.total_download} down / "
            f"{p.total_conflict} conflict / {p.total_no_op} no-op"
        )

    # -- execution (worker thread) -----------------------------------------

    def execute(self, *, dry_run: bool = False) -> None:
        self.phase = "running"
        self.progress = (0, max(1, len(self._op_order())))
        try:
            self.result = execute(
                self.engine,
                self.client,
                self.plan,
                allow_upload=self.allow_upload,
                policy=self.policy,
                dry_run=dry_run,
                on_op=lambda d, t: setattr(self, "progress", (d, t)),
            )
            self.error = ""
            self.phase = "done"
        except Exception as exc:  # noqa: BLE001  surface to the GUI
            self.error = f"{exc}"
            self.phase = "error"

    def _op_order(self) -> list:
        return list(self.plan.operations)