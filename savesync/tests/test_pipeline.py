"""Offline tests for the shared pipeline module  the code path used by both
the CLI and the pygame GUI. No network, no real server.

Covers: scan_negotiate read-only behaviour and platform scoping, execute's
dry-run completing 0/0, allow_upload gating, and the on_op progress callback
used by the GUI progress bar.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from savesync.config import SyncConfig
from savesync.engine import CONFLICT_SKIP
from savesync.pipeline import SyncSession, execute, scan_negotiate

from test_cli import FakeClient


def _tmp_cfg(root: Path) -> SyncConfig:
    return SyncConfig(device_id="dev-ok", device_name="box", save_root=str(root),
                      save_dirs={"gbc": ["saves/gbc"], "*": ["saves"]})


def test_scan_negotiate_is_read_only(tmp_path):
    client = FakeClient(roms={892: ("gbc", 1)})
    cfg = _tmp_cfg(tmp_path)
    eng, plan = scan_negotiate(client, cfg, tmp_path)
    # read-only: plan is produced but nothing downloaded, no session closed
    assert plan.total_download == 1
    assert not any(c.startswith("download:") for c in client.calls)
    assert client.completed == []


def test_scan_negotiate_platform_scope_drops_unmapped_ops(tmp_path):
    # two platforms, but scan scoped to "gbc": the "gba" download (rom 5) must
    # be dropped from the plan so it can't execute into an unresolvable dir.
    client = FakeClient(roms={892: ("gbc", 1), 5: ("gba", 2)})
    client.platforms = [{"id": 1, "fs_slug": "gbc", "slug": "gbc"},
                        {"id": 2, "fs_slug": "gba", "slug": "gba"}]
    cfg = _tmp_cfg(tmp_path)
    # a local save on the scoped platform so the targeted scan resolves rom 892
    save = tmp_path / "saves" / "gbc" / "Game 892.srm"
    save.parent.mkdir(parents=True, exist_ok=True)
    save.write_bytes(b"local")
    _, plan = scan_negotiate(client, cfg, tmp_path, platform="gbc")
    assert {o.rom_id for o in plan.operations} == {892}
    assert plan.total_download == 1


def test_execute_dry_run_is_read_only(tmp_path):
    client = FakeClient(roms={892: ("gbc", 1)})
    cfg = _tmp_cfg(tmp_path)
    eng, plan = scan_negotiate(client, cfg, tmp_path)
    result = execute(eng, client, plan, dry_run=True, policy=CONFLICT_SKIP)
    assert result.downloaded == 0
    assert client.completed == [(99, 0, 0)]  # dry-run closes 0/0
    assert not any(c.startswith("download:") for c in client.calls)


def test_execute_applies_and_completes(tmp_path):
    client = FakeClient(roms={892: ("gbc", 1)})
    cfg = _tmp_cfg(tmp_path)
    eng, plan = scan_negotiate(client, cfg, tmp_path)
    result = execute(eng, client, plan, dry_run=False, policy=CONFLICT_SKIP)
    assert result.downloaded == 1
    assert client.completed == [(99, 1, 0)]


def test_execute_reports_progress_per_op(tmp_path):
    client = FakeClient(roms={892: ("gbc", 1), 5: ("gba", 2)})
    client.platforms = [{"id": 1, "fs_slug": "gbc", "slug": "gbc"},
                        {"id": 2, "fs_slug": "gba", "slug": "gba"}]
    cfg = _tmp_cfg(tmp_path)
    cfg.save_dirs = {"gbc": ["saves/gbc"], "gba": ["saves/gba"], "*": ["saves"]}
    eng, plan = scan_negotiate(client, cfg, tmp_path)
    progress: list[tuple[int, int]] = []
    execute(eng, client, plan, allow_upload=False, policy=CONFLICT_SKIP,
            dry_run=False, on_op=lambda d, t: progress.append((d, t)))
    # two downloads -> final tick (2, 2)
    assert progress and progress[-1] == (2, 2)
    assert len(progress) == 2


# ------------------------------------------------------------------ SyncSession
# The pygame-free controller the GUI drives. These verify the policy cycle,
# conflict preview, upload gate, and the execute error path headlessly.


def test_session_cycles_policy_and_preview(tmp_path):
    # one conflict, one plain download: policy must cycle and the preview must
    # report what each conflict resolves to under the active policy
    client = FakeClient(roms={892: ("gbc", 1)},
                        conflicts=[(892, "srm")])
    cfg = _tmp_cfg(tmp_path)
    eng, plan = scan_negotiate(client, cfg, tmp_path)
    session = SyncSession(eng, plan, client)

    assert session.phase == "idle"
    session.scan()
    # the fake produces no upload ops: conflict replaces the download for rom 892
    assert session.summary.startswith("0 up / 0 down / 1 conflict")

    lines = dict(session.preview_lines)
    assert lines and all(v in ("upload", "download", "skip") for v in lines.values())

    session.set_policy("take_server")
    assert all(v == "download" for v in dict(session.preview_lines).values())
    session.set_policy("keep_local")
    assert all(v == "upload" for v in dict(session.preview_lines).values())
    session.set_policy("skip")
    assert all(v == "skip" for v in dict(session.preview_lines).values())


def test_session_toggle_upload_and_execute_progress(tmp_path):
    client = FakeClient(roms={892: ("gbc", 1)})
    cfg = _tmp_cfg(tmp_path)
    eng, plan = scan_negotiate(client, cfg, tmp_path)
    session = SyncSession(eng, plan, client)

    assert session.allow_upload is True
    assert session.toggle_upload() is False

    session.execute(dry_run=False)
    assert session.phase == "done"
    assert session.result is not None
    assert session.progress[0] == session.progress[1]  # finished ticking


def test_session_execute_surfaces_errors(tmp_path):
    class BoomClient(FakeClient):
        def complete_session(self, session_id, completed, failed):
            raise RuntimeError("boom")

    boom = BoomClient(roms={892: ("gbc", 1)})
    cfg = _tmp_cfg(tmp_path)
    eng, plan = scan_negotiate(boom, cfg, tmp_path)
    session = SyncSession(eng, plan, boom)
    session.execute(dry_run=False)
    assert session.phase == "error"
    assert "boom" in session.error