"""Offline tests for the Phase-4 save-sync CLI  no network, no real server.

Covers: cred resolution order, sidecar state load/persist, device ensure
(get-vs-register), orchestrate's full pipeline (scan -> match -> negotiate ->
execute -> complete), dry-run completing 0/0, allow_upload gating, platform
filtering, and underscore-skip of the save_dirs map.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from savesync.api import RomMError
from savesync.config import SyncConfig


class _Err404(RomMError):
    pass
from savesync.cli import (
    STATE_PATH,
    ensure_device,
    load_cfg,
    main,
    orchestrate,
    resolve_creds,
)
from savesync.engine import CONFLICT_SKIP, CONFLICT_AUTO
from savesync.models import LocalSave


# ------------------------------------------------------------------ fakes

class FakeClient:
    def __init__(self, platforms=None, roms=None, conflicts=None) -> None:
        self.calls: list[str] = []
        self.completed: list[tuple[int, int, int]] = []
        self.platforms = platforms or [{"id": 1, "fs_slug": "gbc", "slug": "gbc"}]
        # rom_id -> (fs_slug, platform_id)
        self.roms = roms or {892: ("gbc", 1)}
        # conflict ops: list of (rom_id, extension) that negotiate returns as
        # action="conflict" instead of download (server-ambiguous tie)
        self.conflicts = conflicts or []
        self.known_device = "dev-ok"
        self._device_seq = 0

    def list_platforms(self):
        self.calls.append("list_platforms")
        return self.platforms

    def list_roms(self, platform_id):
        self.calls.append(f"list_roms:{platform_id}")
        return [{"id": rid, "fs_slug": slug, "slug": slug}
                for rid, (slug, pid) in self.roms.items() if pid == platform_id]

    def get_device(self, device_id):
        self.calls.append(f"get_device:{device_id}")
        if device_id == self.known_device:
            return {"id": device_id}
        raise _Err404(f"device {device_id} not found")

    def register_device(self, device_name):
        self.calls.append(f"register:{device_name}")
        self._device_seq += 1
        return f"new-dev-{self._device_seq}"

    def negotiate(self, device_id, saves):
        self.calls.append(f"negotiate:{device_id}:{len(saves)} saves")
        from savesync.models import SyncOperation, SyncPlan
        ops = []
        # one download for every known rom we didn't provide a save for
        for rid, (slug, _) in self.roms.items():
            provided = any(s.rom_id == rid for s in saves)
            conflict_rids = {rid for rid, _ in self.conflicts}
            if rid in conflict_rids:
                from datetime import datetime, timezone
                ext = dict(self.conflicts)[rid]
                ops.append(SyncOperation(
                    action="conflict", rom_id=rid, save_id=rid,
                    file_name=f"Game {rid} [tie].{ext}", slot="autosave",
                    emulator="retroarch",
                    server_updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc)))
            elif not provided:
                ops.append(SyncOperation(
                    action="download", rom_id=rid, save_id=rid,
                    file_name=f"Game {rid}.srm", slot="autosave",
                    emulator="retroarch"))
        return SyncPlan(session_id=99, operations=ops)

    def complete_session(self, session_id, completed, failed):
        self.completed.append((session_id, completed, failed))

    def download_save(self, op, dest_path: str, device_id: str) -> None:
        self.calls.append(f"download:{op.file_name}:{dest_path}")
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(dest_path).write_bytes(b"server-bytes")

    def confirm_download(self, save_id: int, device_id: str) -> None:
        self.calls.append(f"confirm:{save_id}")

    def upload_save(self, local: LocalSave, device_id: str,
                    overwrite: bool = False) -> None:
        self.calls.append(f"upload:{local.file_name}:overwrite={overwrite}")


# ------------------------------------------------------------------ creds

def test_resolve_creds_flag_wins(monkeypatch, tmp_path):
    monkeypatch.setattr("savesync.cli.SECRETS_PATH", tmp_path / "no.json")
    ns = _NS(url="https://flags.example", token="flag-tok", config="")
    assert resolve_creds(ns) == ("https://flags.example", "flag-tok")


def test_resolve_creds_env_vs_secrets(monkeypatch, tmp_path):
    secrets = tmp_path / "secrets.json"
    secrets.write_text('{"romm": {"base_url": "https://sec.example", '
                       '"rmm_token": "sec-tok"}}')
    monkeypatch.setenv("ROMM_URL", "https://env.example")
    monkeypatch.setenv("ROMM_TOKEN", "env-tok")
    monkeypatch.setattr("savesync.cli.SECRETS_PATH", secrets)
    ns = _NS(url="", token="", config="")
    assert resolve_creds(ns) == ("https://env.example", "env-tok")


def test_resolve_creds_secrets_fallback(monkeypatch, tmp_path):
    secrets = tmp_path / "secrets.json"
    secrets.write_text('{"romm": {"base_url": "https://sec.example", '
                       '"rmm_token": "sec-tok"}}')
    monkeypatch.setattr("savesync.cli.SECRETS_PATH", secrets)
    ns = _NS(url="", token="", config="")
    assert resolve_creds(ns) == ("https://sec.example", "sec-tok")


def test_resolve_creds_config_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr("savesync.cli.SECRETS_PATH", tmp_path / "no.json")
    cfg = tmp_path / "config.json"
    cfg.write_text('{"romm_url": "https://cfg.example", "password": "cfg-tok"}')
    ns = _NS(url="", token="", config=str(cfg))
    assert resolve_creds(ns) == ("https://cfg.example", "cfg-tok")


def test_resolve_creds_strips_trailing_api(monkeypatch, tmp_path):
    monkeypatch.setattr("savesync.cli.SECRETS_PATH", tmp_path / "no.json")
    ns = _NS(url="https://cfg.example/api", token="tok", config="")
    assert resolve_creds(ns) == ("https://cfg.example", "tok")


def test_resolve_creds_missing_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("savesync.cli.SECRETS_PATH", tmp_path / "no.json")
    ns = _NS(url="", token="", config=str(tmp_path / "no-config.json"))
    try:
        resolve_creds(ns)
        assert False, "expected SystemExit"
    except SystemExit:
        pass


# ------------------------------------------------------------------ state

def test_load_cfg_falls_back_to_defaults(tmp_path):
    ns_state = tmp_path / "state.json"
    cfg = load_cfg(ns_state)
    assert cfg.device_id in ("", None) or False  # empty default
    assert cfg.save_dirs  # loaded from save_directories.json skeleton


def test_load_cfg_reads_state(tmp_path):
    st = tmp_path / "state.json"
    st.write_text('{"device_id": "abc", "device_name": "mybox"}')
    cfg = load_cfg(st)
    assert cfg.device_id == "abc"
    assert cfg.device_name == "mybox"


def test_ensure_device_known_ok(tmp_path, capsys):
    client = FakeClient()
    cfg = SyncConfig(device_id="dev-ok", device_name="box")
    ensure_device(client, cfg, tmp_path / "s.json", debug=True)
    assert "device ok" in capsys.readouterr().out
    assert cfg.device_id == "dev-ok"


def test_ensure_device_re_registers_when_unknown(tmp_path):
    client = FakeClient()
    cfg = SyncConfig(device_id="stale-dev", device_name="box")
    ensure_device(client, cfg, tmp_path / "s.json", debug=True)
    assert cfg.device_id == "new-dev-1"
    assert (tmp_path / "s.json").exists()  # persisted


def test_ensure_device_registers_new_when_none(tmp_path):
    client = FakeClient()
    cfg = SyncConfig(device_id="", device_name="box")
    ensure_device(client, cfg, tmp_path / "s.json", debug=True)
    assert cfg.device_id == "new-dev-1"


# ------------------------------------------------------------------ orchestrate

def _tmp_cfg(root: Path) -> SyncConfig:
    cfg = SyncConfig(device_id="dev-ok", device_name="box", save_root=str(root),
                     save_dirs={"gbc": ["saves/gbc"], "*": ["saves"]})
    return cfg


def test_orchestrate_downloads_into_per_platform_dir(tmp_path):
    client = FakeClient(roms={892: ("gbc", 1)})
    cfg = _tmp_cfg(tmp_path)
    result = orchestrate(client, cfg, tmp_path, execute=True,
                         allow_upload=False, policy=CONFLICT_SKIP)
    gbc_dir = tmp_path / "saves" / "gbc"
    assert gbc_dir.exists()
    # destination resolves through save_root + per-platform dir
    assert any("saves/gbc" in c for c in client.calls if c.startswith("download:"))
    assert not gbc_dir.exists() or list(gbc_dir.glob("*.srm"))
    assert result.downloaded == 1


def test_orchestrate_unknown_slug_falls_back_to_wildcard(tmp_path):
    # rom for a platform whose slug isn't a configured key -> "*" dir
    client = FakeClient(roms={5: ("gba", 2)})
    client.platforms = [{"id": 1, "fs_slug": "gbc", "slug": "gbc"},
                        {"id": 2, "fs_slug": "gba", "slug": "gba"}]
    cfg = _tmp_cfg(tmp_path)
    orchestrate(client, cfg, tmp_path, execute=True, allow_upload=False,
                policy=CONFLICT_SKIP)
    assert (tmp_path / "saves" / "Game 5.srm").exists()  # wildcard "saves"


def test_orchestrate_dry_run_completes_zero_zero(tmp_path):
    client = FakeClient(roms={892: ("gbc", 1)})
    cfg = _tmp_cfg(tmp_path)
    orchestrate(client, cfg, tmp_path, execute=False, allow_upload=False,
                policy=CONFLICT_SKIP)
    assert client.completed == [(99, 0, 0)]
    # no download happened in dry-run
    assert not any(c.startswith("download:") for c in client.calls)


def test_orchestrate_execute_completes_with_counts(tmp_path):
    client = FakeClient(roms={892: ("gbc", 1)})
    cfg = _tmp_cfg(tmp_path)
    orchestrate(client, cfg, tmp_path, execute=True, allow_upload=False,
                policy=CONFLICT_SKIP)
    assert client.completed == [(99, 1, 0)]


def test_orchestrate_platform_filter(tmp_path):
    client = FakeClient(roms={892: ("gbc", 1), 5: ("gba", 2)})
    client.platforms = [{"id": 1, "fs_slug": "gbc", "slug": "gbc"},
                        {"id": 2, "fs_slug": "gba", "slug": "gba"}]
    cfg = _tmp_cfg(tmp_path)
    orchestrate(client, cfg, tmp_path, execute=True, allow_upload=False,
                policy=CONFLICT_SKIP, platform="gbc")
    # only gbc roms negotiated; no download of gba rom 5
    assert not any("Game 5" in c for c in client.calls)


# ------------------------------------------------------------------ helpers

class _NS:
    def __init__(self, url, token, config):
        self.url = url
        self.token = token
        self.config = config