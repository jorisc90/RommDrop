"""Config handling for the save-sync extension.

Extends `romm_drop.py`'s config.json with optional sync settings. Nothing
breaks if they're absent  the extension degrades to read-only audit mode.
"""

from __future__ import annotations

import json
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
SAVE_DIRS_PATH = Path(__file__).resolve().parent / "save_directories.json"

# Sync settings that may (optionally) live in config.json under "savesync".
OPTIONAL_KEYS = ("device_id", "device_name", "save_dirs", "sync_emulator", "max_saves")


@dataclass
class SyncConfig:
    device_id: str = ""
    device_name: str = ""
    save_dirs: dict[str, list[str]] = field(default_factory=dict)  # fs_slug -> [dirs]
    save_root: str = ""                 # base dir; empty = cwd for relative save_dirs
    sync_emulator: str = "retroarch"  # tagged on uploaded saves
    config_path: Path = CONFIG_PATH

    @classmethod
    def load(cls, config_path: Path = CONFIG_PATH) -> "SyncConfig":
        cfg: dict = {}
        try:
            with open(config_path) as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

        sc = cfg.get("savesync", {})
        device_id = sc.get("device_id") or _default_device_id()
        return cls(
            device_id=device_id,
            device_name=sc.get("device_name", socket.gethostname() or "rommdrop"),
            save_dirs=sc.get("save_dirs", {}),
            save_root=sc.get("save_root", ""),
            sync_emulator=sc.get("sync_emulator", "retroarch"),
            config_path=config_path,
        )

    def persist_device_id(self) -> None:
        """Write device_id (and name) back into config.json under 'savesync'."""
        cfg: dict = {}
        try:
            with open(self.config_path) as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
        cfg.setdefault("savesync", {})
        cfg["savesync"]["device_id"] = self.device_id
        cfg["savesync"]["device_name"] = self.device_name
        with open(self.config_path, "w") as f:
            json.dump(cfg, f, indent=2)


def _default_device_id() -> str:
    """Stable-ish device id: persisted on first successful registration."""
    return f"rommdrop-{uuid.uuid4().hex[:12]}"


def load_save_dirs() -> dict[str, list[str]]:
    """Load the fs_slug -> save-directory mapping (skeleton, user-editable)."""
    try:
        with open(SAVE_DIRS_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
