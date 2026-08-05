"""Data models for RomM save/state synchronization.

These mirror the wire contract used by the RomM 5.0+ sync orchestrator
(verified against the `rommapp/grout` Go client, `romm/*.go`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class LocalSave:
    """A save or savestate file found on this device (pre-negotiate state)."""

    rom_id: int | None = None          # resolved by matcher; None = unmatched yet
    file_name: str = ""
    file_path: str = ""                # absolute path on this device
    slot: str | None = None            # "autosave" (default) or named slot for savestates
    emulator: str = ""
    content_hash: str = ""
    updated_at: datetime | None = None
    file_size_bytes: int = 0

    def to_manifest(self) -> dict:
        """Serialize to a `ClientSaveState` as expected by /api/sync/negotiate."""
        return {
            "rom_id": self.rom_id,
            "file_name": self.file_name,
            "slot": self.slot,
            "emulator": self.emulator,
            "content_hash": self.content_hash,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "file_size_bytes": self.file_size_bytes,
        }


@dataclass
class ServerSave:
    """A save record as returned by RomM (`Save` in grout's romm/saves.go)."""

    id: int = 0
    rom_id: int = 0
    file_name: str = ""
    file_size_bytes: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    emulator: str = ""
    slot: str | None = None
    content_hash: str | None = None
    file_path: str = ""

    @classmethod
    def from_json(cls, data: dict) -> "ServerSave":
        return cls(
            id=int(data.get("id") or 0),
            rom_id=int(data.get("rom_id") or 0),
            file_name=data.get("file_name", ""),
            file_size_bytes=int(data.get("file_size_bytes") or 0),
            created_at=_parse_dt(data.get("created_at")),
            updated_at=_parse_dt(data.get("updated_at")),
            emulator=data.get("emulator", ""),
            slot=data.get("slot"),
            content_hash=data.get("content_hash"),
            file_path=data.get("file_path", ""),
        )


@dataclass
class SyncOperation:
    """One operation returned by the server in a negotiate plan."""

    action: str = "no_op"              # upload | download | conflict | no_op
    rom_id: int = 0
    save_id: int | None = None
    file_name: str = ""
    slot: str | None = None
    emulator: str = ""
    reason: str = ""
    server_updated_at: datetime | None = None
    server_content_hash: str | None = None
    resolved_as: str | None = None   # set by engine: upload|download|skip for conflicts

    @classmethod
    def from_json(cls, data: dict) -> "SyncOperation":
        return cls(
            action=data.get("action", "no_op"),
            rom_id=int(data.get("rom_id") or 0),
            save_id=data.get("save_id"),
            file_name=data.get("file_name", ""),
            slot=data.get("slot"),
            emulator=data.get("emulator", ""),
            reason=data.get("reason", ""),
            server_updated_at=_parse_dt(data.get("server_updated_at")),
            server_content_hash=data.get("server_content_hash"),
        )


@dataclass
class SyncPlan:
    """Full negotiate response."""

    session_id: int = 0
    operations: list[SyncOperation] = field(default_factory=list)
    total_upload: int = 0
    total_download: int = 0
    total_conflict: int = 0
    total_no_op: int = 0

    @classmethod
    def from_json(cls, data: dict) -> "SyncPlan":
        return cls(
            session_id=int(data.get("session_id") or 0),
            operations=[SyncOperation.from_json(o) for o in data.get("operations", [])],
            total_upload=int(data.get("total_upload") or 0),
            total_download=int(data.get("total_download") or 0),
            total_conflict=int(data.get("total_conflict") or 0),
            total_no_op=int(data.get("total_no_op") or 0),
        )

    @property
    def total(self) -> int:
        return self.total_upload + self.total_download + self.total_conflict + self.total_no_op


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
