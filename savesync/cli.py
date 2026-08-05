#!/usr/bin/env python3
"""RommDrop save-sync CLI.

Runs the full sync pipeline from the terminal (no pygame needed):

    python savesync/cli.py register
    python savesync/cli.py plan [--root DIR] [--platform gbc]
    python savesync/cli.py sync  [--allow-upload] [--policy auto] [--root DIR]

Pipeline: scan local saves -> match to server ROMs -> negotiate -> execute ->
complete session. `plan` is always read-only (negotiate + complete 0/0, no
disk/network writes). `sync` executes unless --dry-run.

Credentials are resolved in this order:
  1. --url / --token flags
  2. ROMM_URL / ROMM_TOKEN environment variables
  3. ~/.hermes/secrets.json  romm.* (base_url / token / rmm_token / api_key)
  4. RommDrop/config.json (romm_url + password-as-token)

Persistent state (device_id/device_name) is kept in RommDrop/savesync_state.json
so the template config.json is never rewritten.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# allow `python savesync/cli.py` and `python -m savesync.cli`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from savesync.api import RomMClient, RomMError
from savesync.config import SyncConfig, load_save_dirs
from savesync.engine import (
    CONFLICT_AUTO,
    CONFLICT_KEEP_LOCAL,
    CONFLICT_SKIP,
    CONFLICT_TAKE_SERVER,
    SyncEngine,
)

SCRIPT_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = SCRIPT_DIR / "savesync_state.json"
SECRETS_PATH = Path.home() / ".hermes" / "secrets.json"

POLICIES = {
    "auto": CONFLICT_AUTO,
    "keep_local": CONFLICT_KEEP_LOCAL,
    "take_server": CONFLICT_TAKE_SERVER,
    "skip": CONFLICT_SKIP,
}


# ------------------------------------------------------------------ creds


def _env_or_flag(flag: str, env: str) -> str | None:
    if flag:
        return flag
    return os.environ.get(env) or None


def discover_creds(url: str = "", token: str = "", config: str = "") -> tuple[str, str]:
    """Same fallback chain as resolve_creds, but callable with plain strings.

    Used by the GUI (no argparse). Returns (base_url, token), stripping any
    trailing /api. Raises SystemExit if unresolvable  the caller should catch
    it and surface the message to the user.
    """
    ns = argparse.Namespace(url=url, token=token, config=config)
    return resolve_creds(ns)


def resolve_creds(args: argparse.Namespace) -> tuple[str, str]:
    """Return (base_url, token), raising SystemExit if unresolvable."""
    url = _env_or_flag(args.url, "ROMM_URL")
    token = _env_or_flag(args.token, "ROMM_TOKEN")

    if not (url and token):
        # 3. secrets.json romm.* (dev box)
        try:
            with open(SECRETS_PATH) as f:
                romm = json.load(f).get("romm", {})
            url = url or romm.get("base_url") or romm.get("url")
            token = token or (romm.get("rmm_token") or romm.get("token")
                              or romm.get("api_key"))
        except (OSError, json.JSONDecodeError):
            romm = {}

    if not (url and token):
        # 4. config.json (romm_url + password-as-token)
        cfg_path = Path(args.config) if args.config else SCRIPT_DIR / "config.json"
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
            url = url or cfg.get("romm_url")
            token = token or cfg.get("password")
        except (OSError, json.JSONDecodeError):
            pass

    if not url:
        sys.exit("error: no RomM URL (pass --url, set ROMM_URL, or fill config.json)")
    if not token:
        sys.exit("error: no token (pass --token, set ROMM_TOKEN, or fill config.json)")
    url = url.rstrip("/")
    if url.endswith("/api"):
        url = url[:-4]
    return url, token


# ------------------------------------------------------------------ device


def ensure_device(client: RomMClient, cfg: SyncConfig, state_path: Path,
                  debug: bool = False) -> None:
    """Verify the persisted device id, or register + persist a new one."""
    if cfg.device_id:
        try:
            client.get_device(cfg.device_id)
            if debug:
                print(f"[cli] device ok: {cfg.device_id}")
            return
        except RomMError:
            print(f"[cli] device {cfg.device_id!r} unknown on server; re-registering")
    new_id = client.register_device(cfg.device_name)
    cfg.device_id = new_id
    try:
        with open(state_path, "w") as f:
            json.dump({"device_id": cfg.device_id,
                       "device_name": cfg.device_name}, f, indent=2)
    except OSError as exc:
        print(f"[cli] warning: could not persist device state: {exc}")
    print(f"[cli] registered device {new_id!r} ({cfg.device_name})")


def load_cfg(state_path: Path) -> SyncConfig:
    """SyncConfig from the sidecar state file (falls back to defaults)."""
    try:
        with open(state_path) as f:
            st = json.load(f)
        cfg = SyncConfig(device_id=st.get("device_id", ""),
                         device_name=st.get("device_name", "rommdrop-cli"))
    except (OSError, json.JSONDecodeError):
        cfg = SyncConfig(device_name="rommdrop-cli")
    cfg.save_dirs = load_save_dirs() or cfg.save_dirs
    return cfg


# ------------------------------------------------------------------ pipeline


def orchestrate(client: RomMClient, cfg: SyncConfig, root: Path, *,
                execute: bool, allow_upload: bool, policy: str,
                platform: str | None = None, debug: bool = False):
    """Full pipeline. Returns the SyncResult (plan attached)."""
    from .pipeline import execute as run_plan, scan_negotiate

    eng, plan = scan_negotiate(client, cfg, root, platform=platform,
                               debug=debug)
    print(f"[cli] negotiated: {plan.total_upload} up, {plan.total_download} down, "
          f"{plan.total_conflict} conflict, {plan.total_no_op} noop "
          f"(session {plan.session_id})")

    if plan.total_conflict and not execute:
        preview = eng.preview_conflicts(plan, policy)
        for op, how in preview:
            print(f"[cli]   conflict {op.file_name} (rom {op.rom_id}) -> {how}")

    return run_plan(eng, client, plan, dry_run=not execute,
                    allow_upload=allow_upload, policy=policy)


# ------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="savesync",
                                description="RomM save sync CLI (scan->negotiate->execute)")
    p.add_argument("--url", help="RomM base URL (overrides env/config)")
    p.add_argument("--token", help="RomM API token (overrides env/config)")
    p.add_argument("--config", help="path to config.json (default: RommDrop/config.json)")
    p.add_argument("--root", help="save root dir (default: cwd)")
    p.add_argument("--platform", help="only sync one fs_slug, e.g. gbc")
    p.add_argument("--debug", action="store_true", help="verbose engine output")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("register", help="verify or register the sync device")
    sub.add_parser("plan", help="scan + negotiate only (read-only, no writes)")
    s = sub.add_parser("sync", help="scan, negotiate, execute")
    s.add_argument("--no-upload", dest="allow_upload", action="store_false",
                   help="disable uploads (default: uploads enabled)")
    s.add_argument("--allow-upload", dest="allow_upload", action="store_true",
                   help=argparse.SUPPRESS)
    s.set_defaults(allow_upload=True)
    s.add_argument("--policy", choices=sorted(POLICIES), default="auto",
                   help="conflict policy (default: auto)")
    s.add_argument("--dry-run", action="store_true",
                   help="negotiate + preview, do not execute")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    url, token = resolve_creds(args)
    root = Path(args.root).resolve() if args.root else Path.cwd()
    cfg = load_cfg(STATE_PATH)
    cfg.save_root = str(root)  # _dest_path resolves relative save_dirs here
    client = RomMClient(url, ("", token), timeout=30)

    if args.command == "register":
        ensure_device(client, cfg, STATE_PATH, debug=args.debug)
        return 0

    execute = args.command == "sync" and not args.dry_run
    ensure_device(client, cfg, STATE_PATH, debug=args.debug)
    policy = POLICIES[args.policy] if args.command == "sync" else CONFLICT_SKIP
    allow_upload = getattr(args, "allow_upload", False)
    result = orchestrate(client, cfg, root, execute=execute,
                         allow_upload=allow_upload, policy=policy,
                         platform=args.platform, debug=args.debug)

    print(f"[cli] done: {result.uploaded} uploaded, {result.downloaded} downloaded, "
          f"{result.conflicts} conflicts, {result.no_ops} noop")
    if result.failed:
        print(f"[cli] {len(result.failed)} FAILED:")
        for f_ in result.failed:
            print(f"[cli]   - {f_}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
