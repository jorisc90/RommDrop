"""Local save/state discovery and hashing (Phase 1 core).

Pure filesystem work, no network. Produces LocalSave records with rom_id
unset; matching to RomM ROMs happens via `match_local_to_rom` (uses the
`fs_name` field, verified against RomM 5.1.0).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from .models import LocalSave

# Extensions treated as save files vs savestates. RomM convention (verified
# against the 5.1.0 server): battery saves live under slot "autosave",
# savestates use the trailing slot digit (.state1 -> "1").
SAVE_EXTS = {".srm", ".sav", ".mcr", ".mcd", ".eep", ".sra", ".nv",
             ".srm.auto", ".auto", ".fla", ".bkr", ".mp"}
STATE_EXTS = {".state", ".state0", ".state1", ".state2", ".state3",
              ".state4", ".state5", ".state6", ".state7", ".state8", ".state9"}
AUTOSAVE_SLOT = "autosave"

EMULATOR_BY_EXT = {
    ".srm": "retroarch",
    ".state": "retroarch",
    ".sav": "retroarch",
    ".sram": "retroarch",
    ".mcr": "mupen64plus",
    ".mcd": "pcsx2",
    ".nvm": "pcsx2",
    ".dolphin": "dolphin",
}


def scan_save_dirs(root: Path, save_dirs_map: dict[str, list[str]],
                   platform_fs_slug: str) -> list[LocalSave]:
    """Scan the mapped directories for a platform and hash each result.

    Unknown platforms fall back to the "*" wildcard entry when present.
    """
    roots = _dirs_for(save_dirs_map, platform_fs_slug)
    found: list[LocalSave] = []
    for rel in roots:
        base = (root / rel) if not Path(rel).is_absolute() else Path(rel)
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            if ext not in SAVE_EXTS and ext not in STATE_EXTS:
                continue
            found.append(_file_to_local(p))
    return found


def _dirs_for(mapping: dict[str, list[str]], slug: str) -> list[str]:
    if slug in mapping:
        return mapping[slug]
    if "*" in mapping:
        return mapping["*"]
    return []


def _file_to_local(p: Path) -> LocalSave:
    ext = p.suffix.lower()
    stat = p.stat()
    return LocalSave(
        file_name=p.name,
        file_path=str(p),
        slot=slot_from_ext(ext),
        emulator=emulator_for(ext),
        content_hash=hash_file(p),
        updated_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        file_size_bytes=stat.st_size,
    )


def slot_from_ext(ext: str) -> str | None:
    """RomM slot convention (verified against server 5.1.0):
    battery/save files -> "autosave"; savestates -> trailing digit
    (.state1 -> "1", plain .state -> None)."""
    if ext in SAVE_EXTS:
        return AUTOSAVE_SLOT
    if ext.startswith(".state"):
        suffix = ext[len(".state"):]
        return suffix or None
    return None


def emulator_for(ext: str) -> str:
    return EMULATOR_BY_EXT.get(ext, "retroarch")


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


# ------------------------------------------------------------------ matching

# Tags removed from both sides before comparing: [bracketed] (timestamps,
# upload markers) and (region/language) tags like (World) (En,Ja).
_TAG_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)")


def match_local_to_rom(local_saves: list[LocalSave], roms: list[dict]) -> int:
    """Resolve rom_id for unmatched saves by matching the normalized stem of
    the local file against ROM file names on the same platform.

    RomM 5.1 rom objects use `fs_name` (e.g. "Wario Land 3 (World) (En,Ja).zip");
    server-side saves additionally carry [upload-timestamp] tags. Both sides
    are normalized (bracket/paren tags stripped, lowercased, spaces removed)
    before the stem compare. Returns count matched.
    """
    lookup: dict[str, int] = {}
    for rom in roms:
        rid = int(rom.get("id") or 0)
        for key in _rom_stems(rom):
            lookup.setdefault(key, rid)

    matched = 0
    for ls in local_saves:
        stem = normalize_stem(ls.file_name)
        if stem in lookup:
            ls.rom_id = lookup[stem]
            matched += 1
    return matched


def _rom_stems(rom: dict) -> list[str]:
    stems: list[str] = []
    for fn in (rom.get("fs_name"), rom.get("file_name"), rom.get("name")):
        if fn:
            stems.append(normalize_stem(str(fn)))
    for f in rom.get("files", []):  # multi-part archives
        name = f.get("file_name") or f.get("name")
        if name:
            stems.append(normalize_stem(str(name)))
    return stems


def normalize_stem(name: str) -> str:
    """'Wario Land 3 (World) (En,Ja) [2026-08-05_15-14-06].srm'
    -> 'warioland3'  stable across save/rom naming differences."""
    base = name.rsplit(".", 1)[0]
    stripped = _TAG_RE.sub("", base)
    return re.sub(r"[^a-z0-9]", "", stripped.lower())


# Server saves carry a "[upload-timestamp]" tag appended to the base name
# (e.g. "Wario Land 3 (World) (En,Ja) [2026-08-05_15-14-06].srm"). When
# writing a download to disk, strip that tag so the emulator sees a clean
# name matching the ROM ("Wario Land 3 (World) (En,Ja).srm").
_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]")
_EXT_RE = re.compile(r"^(.*)(\.[^.]+)$", re.S)


def clean_server_filename(server_name: str) -> str:
    """Strip a server-side [timestamp] tag from a save filename."""
    m = _EXT_RE.match(server_name)
    if not m:
        name, ext = server_name, ""
    else:
        name, ext = m.group(1), m.group(2)
    return _BRACKET_RE.sub("", name) + ext
