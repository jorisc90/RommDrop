"""Offline Phase-3 conflict tests  no network, no real RomM server.

Covers: download / upload / no_op apply paths, all four conflict policies
(keep_local, take_server, auto, skip), the allow_upload gate, and the
tag-normalized local-save match that conflicts depend on.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from savesync.config import SyncConfig
from savesync.engine import (
    CONFLICT_AUTO,
    CONFLICT_KEEP_LOCAL,
    CONFLICT_SKIP,
    CONFLICT_TAKE_SERVER,
    SyncEngine,
)
from savesync.models import LocalSave, SyncOperation, SyncPlan


# ------------------------------------------------------------------ fakes

class FakeClient:
    """Records calls; serves canned save content for downloads."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.completed: list[tuple[int, int, int]] = []

    def download_save(self, op, dest_path: str, device_id: str) -> None:
        self.calls.append(f"download:{op.file_name}:{dest_path}")
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(dest_path).write_bytes(b"server-bytes")

    def confirm_download(self, save_id: int, device_id: str) -> None:
        self.calls.append(f"confirm:{save_id}")

    def upload_save(self, local: LocalSave, device_id: str,
                    overwrite: bool = False) -> None:
        self.calls.append(f"upload:{local.file_name}:overwrite={overwrite}")

    def complete_session(self, session_id: int, completed: int, failed: int) -> None:
        self.completed.append((session_id, completed, failed))


# ------------------------------------------------------------------ fixtures

def make_engine(tmp: Path, client: FakeClient) -> SyncEngine:
    cfg = SyncConfig(
        device_id="dev-1",
        device_name="test-box",
        save_root=str(tmp),
        save_dirs={"gb": ["saves"]},
    )
    eng = SyncEngine(client, cfg)  # type: ignore[arg-type]
    eng.set_platform_map({1: "gb", 2: "gb"})
    return eng


def local_save(tmp: Path, name: str, rom_id: int,
               updated: datetime | None = None) -> LocalSave:
    p = tmp / "saves" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"local-bytes-" + name.encode())
    return LocalSave(
        rom_id=rom_id,
        file_name=name,
        file_path=str(p),
        slot="autosave",
        emulator="retroarch",
        content_hash="localhash",
        updated_at=updated or datetime(2026, 1, 1, tzinfo=timezone.utc),
        file_size_bytes=p.stat().st_size,
    )


def op(**kw: object) -> SyncOperation:
    base: dict = dict(action="no_op", rom_id=1,
                      file_name="Game (World).srm",
                      slot="autosave", emulator="retroarch")
    base.update(kw)
    return SyncOperation(
        action=str(base["action"]),
        rom_id=int(base["rom_id"]),
        save_id=int(base["save_id"]) if base.get("save_id") else None,
        file_name=str(base["file_name"]),
        slot=str(base["slot"]) if base.get("slot") else None,
        emulator=str(base["emulator"]),
        server_updated_at=base.get("server_updated_at"),
        resolved_as=base.get("resolved_as"),
    )


def plan(*ops: SyncOperation) -> SyncPlan:
    return SyncPlan(session_id=42, operations=list(ops))


# ------------------------------------------------------------------ tests

def test_download_path(tmp: Path) -> None:
    client = FakeClient()
    eng = make_engine(tmp, client)
    res = eng.run(plan(op(action="download", save_id=7,
                           file_name="Game (World) [2026-08-05_10-00-00].srm")),
                  dry_run=False)
    assert res.downloaded == 1, res
    assert client.calls == [
        "download:Game (World) [2026-08-05_10-00-00].srm:" +
        str(tmp / "saves" / "Game (World).srm"),
        "confirm:7",
    ]
    # content landed on disk
    assert (tmp / "saves" / "Game (World).srm").read_bytes() == b"server-bytes"


def test_upload_gated_by_allow_upload(tmp: Path) -> None:
    client = FakeClient()
    eng = make_engine(tmp, client)
    eng.set_scanned_saves([local_save(tmp, "Game (World).srm", 1)])
    # gate closed -> no-op, no upload call
    res = eng.run(plan(op(action="upload", rom_id=1)), dry_run=False,
                  allow_upload=False)
    assert res.uploaded == 0 and res.no_ops == 1, res
    assert client.calls == []
    # gate open -> upload happens, no overwrite for plain upload
    res = eng.run(plan(op(action="upload", rom_id=1)), dry_run=False,
                  allow_upload=True)
    assert res.uploaded == 1, res
    assert client.calls == ["upload:Game (World).srm:overwrite=False"]


def test_no_op(tmp: Path) -> None:
    client = FakeClient()
    eng = make_engine(tmp, client)
    res = eng.run(plan(op(action="no_op")), dry_run=False)
    assert res.no_ops == 1 and res.uploaded == 0 and res.downloaded == 0


def test_conflict_keep_local(tmp: Path) -> None:
    client = FakeClient()
    eng = make_engine(tmp, client)
    # local file has the CLEAN name; server op carries the [timestamp] tag
    eng.set_scanned_saves([local_save(tmp, "Game (World).srm", 1)])
    c = op(action="conflict", save_id=7,
           file_name="Game (World) [2026-08-04_10-00-00].srm",
           server_updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    res = eng.run(plan(c), dry_run=False, policy=CONFLICT_KEEP_LOCAL,
                  allow_upload=True)
    assert res.conflicts == 1 and res.uploaded == 1, res
    assert client.calls == ["upload:Game (World).srm:overwrite=True"]
    assert c.resolved_as == "upload"
    # with the upload gate closed, keep_local cannot act -> skip
    client2 = FakeClient()
    eng2 = make_engine(tmp, client2)
    eng2.set_scanned_saves([local_save(tmp, "Game (World).srm", 1)])
    res2 = eng2.run(plan(op(action="conflict", save_id=7,
                            file_name="Game (World) [2026-08-04_10-00-00].srm")),
                    dry_run=False, policy=CONFLICT_KEEP_LOCAL,
                    allow_upload=False)
    assert res2.uploaded == 0 and res2.downloaded == 0
    assert client2.calls == []


def test_conflict_take_server(tmp: Path) -> None:
    client = FakeClient()
    eng = make_engine(tmp, client)
    c = op(action="conflict", save_id=7,
           file_name="Game (World) [2026-08-04_10-00-00].srm")
    res = eng.run(plan(c), dry_run=False, policy=CONFLICT_TAKE_SERVER)
    assert res.downloaded == 1 and res.conflicts == 1, res
    assert client.calls[0].startswith("download:")
    assert "confirm:7" in client.calls
    assert c.resolved_as == "download"
    assert (tmp / "saves" / "Game (World).srm").exists()


def test_conflict_auto_newest_wins(tmp: Path) -> None:
    # server newer -> download
    client = FakeClient()
    eng = make_engine(tmp, client)
    eng.set_scanned_saves([
        local_save(tmp, "Game (World).srm", 1,
                   updated=datetime(2026, 1, 1, tzinfo=timezone.utc)),
    ])
    c = op(action="conflict", save_id=7,
           file_name="Game (World) [2026-08-04_10-00-00].srm",
           server_updated_at=datetime(2026, 1, 3, tzinfo=timezone.utc))
    res = eng.run(plan(c), dry_run=False, policy=CONFLICT_AUTO)
    assert res.downloaded == 1, res
    assert c.resolved_as == "download"

    # local newer -> upload overwrite
    client2 = FakeClient()
    eng2 = make_engine(tmp, client2)
    eng2.set_scanned_saves([
        local_save(tmp, "Game (World).srm", 1,
                   updated=datetime(2026, 1, 10, tzinfo=timezone.utc)),
    ])
    c2 = op(action="conflict", save_id=7,
            file_name="Game (World) [2026-08-04_10-00-00].srm",
            server_updated_at=datetime(2026, 1, 3, tzinfo=timezone.utc))
    res2 = eng2.run(plan(c2), dry_run=False, policy=CONFLICT_AUTO,
                    allow_upload=True)
    assert res2.uploaded == 1, res2
    assert client2.calls == ["upload:Game (World).srm:overwrite=True"]
    assert c2.resolved_as == "upload"


def test_conflict_skip(tmp: Path) -> None:
    client = FakeClient()
    eng = make_engine(tmp, client)
    c = op(action="conflict", save_id=7)
    res = eng.run(plan(c), dry_run=False, policy=CONFLICT_SKIP)
    assert res.conflicts == 1 and res.uploaded == 0 and res.downloaded == 0
    assert client.calls == []
    assert c.resolved_as == "skip"


def test_preview_conflicts(tmp: Path) -> None:
    eng = make_engine(tmp, FakeClient())
    eng.set_scanned_saves([
        local_save(tmp, "Game (World).srm", 1,
                   updated=datetime(2026, 1, 1, tzinfo=timezone.utc)),
    ])
    c_old = op(action="conflict", save_id=7,
               file_name="Game (World) [2026-08-04_10-00-00].srm",
               server_updated_at=datetime(2026, 1, 3, tzinfo=timezone.utc))
    c_new = op(action="conflict", save_id=8,
               file_name="Other [2026-08-04_10-00-00].srm",
               server_updated_at=datetime(2025, 12, 1, tzinfo=timezone.utc))
    p = plan(c_old, c_new, op(action="download", save_id=9))

    keep = eng.preview_conflicts(p, CONFLICT_KEEP_LOCAL)
    assert [r for _, r in keep] == ["upload", "upload"]
    srv = eng.preview_conflicts(p, CONFLICT_TAKE_SERVER)
    assert [r for _, r in srv] == ["download", "download"]
    auto = eng.preview_conflicts(p, CONFLICT_AUTO)
    assert [r for _, r in auto] == ["download", "upload"]  # newest wins each
    skp = eng.preview_conflicts(p, CONFLICT_SKIP)
    assert [r for _, r in skp] == ["skip", "skip"]


def test_dry_run_no_side_effects(tmp: Path) -> None:
    client = FakeClient()
    eng = make_engine(tmp, client)
    res = eng.run(plan(op(action="download", save_id=7),
                       op(action="upload", rom_id=1),
                       op(action="conflict", save_id=9)),
                  dry_run=True)
    assert res.downloaded == res.uploaded == 0
    assert client.calls == [] and client.completed == []


def test_unknown_policy_raises(tmp: Path) -> None:
    client = FakeClient()
    eng = make_engine(tmp, client)
    eng.set_scanned_saves([local_save(tmp, "Game (World).srm", 1)])
    try:
        eng.run(plan(op(action="conflict", save_id=7)), dry_run=False,
                policy="bogus")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown policy")


def test_complete_session_finalized(tmp: Path) -> None:
    client = FakeClient()
    eng = make_engine(tmp, client)
    eng.set_scanned_saves([local_save(tmp, "Game (World).srm", 1)])
    res = eng.run(plan(op(action="download", save_id=7, file_name="Game (World).srm"),
                       op(action="upload", rom_id=1)),
                  dry_run=False, allow_upload=True)
    assert res.downloaded == 1 and res.uploaded == 1
    assert client.completed == [(42, 2, 0)]


# ------------------------------------------------------------------ runner

def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    with tempfile.TemporaryDirectory() as td:
        for t in tests:
            tmp = Path(td) / t.__name__
            tmp.mkdir(parents=True)
            try:
                t(tmp)
                print(f"PASS {t.__name__}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {t.__name__}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"ERROR {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
