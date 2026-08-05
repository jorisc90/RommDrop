"""Save/state synchronization extension for RommDrop.

Wire-up (Phase 1):
    from savesync import SyncEngine, RomMClient, SyncConfig
    cfg = SyncConfig.load()
    client = RomMClient(base_url, auth)          # reuse romm_drop.py's values
    engine = SyncEngine(client, cfg)
    local = engine.scan_platform(root, fs_slug)  # e.g. Path(retrobat_root), "nes"
    engine.match(local, roms)                    # roms = GET /platforms/{id}/roms
    plan = client.negotiate(cfg.device_id, local)
    engine.run(plan, dry_run=True)               # Phase 1: report only
"""

from __future__ import annotations

from .api import RomMClient, RomMError
from .config import SyncConfig, load_save_dirs
from .engine import (DEFAULT_CONFLICT_POLICY, CONFLICT_AUTO, CONFLICT_KEEP_LOCAL,
                     CONFLICT_SKIP, CONFLICT_TAKE_SERVER, SyncEngine, SyncResult)
from .models import LocalSave, ServerSave, SyncOperation, SyncPlan
from .scanner import hash_file, match_local_to_rom, scan_save_dirs

__all__ = [
    "CONFLICT_AUTO", "CONFLICT_KEEP_LOCAL", "CONFLICT_SKIP", "CONFLICT_TAKE_SERVER",
    "DEFAULT_CONFLICT_POLICY",
    "LocalSave", "RomMClient", "RomMError", "ServerSave", "SyncConfig",
    "SyncEngine", "SyncOperation", "SyncPlan", "SyncResult",
    "hash_file", "load_save_dirs", "match_local_to_rom", "scan_save_dirs",
]
