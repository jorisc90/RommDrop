# RommDrop Save / State Sync Extension  Design Doc

**Status:** Save-sync engine implemented, headless CLI
(`savesync/cli.py`: `register` / `plan` / `sync`) and the pygame GUI sync
screen in `romm_drop.py` are both live-verified and covered by 40 tests.
The pipeline is refactored into `savesync/pipeline.py` (`scan_negotiate` +
`execute` + a pygame-free `SyncSession`) shared by CLI and GUI  see
INTEGRATION.md.
**Target:** RomM server **5.0+** (device-aware saves + sync orchestrator API). Grout v5.0.0.0 pins this same contract.

---

## 1. Why extend RommDrop (and why it's low-risk)

RommDrop is a single-file Pygame GUI (`romm_drop.py`, ~400 lines) that already:

- holds RomM server auth (`config.json` → `BASE_URL`, `AUTH`),
- resolves platforms and ROMs over `GET /api/roms`,
- downloads ROMs into `<RETROBAT_ROOT>/<platform_fs_slug>/<file>`.

Save syncing is the *mirror image* of that last step. Crucially, **the hard half is server-side**: RomM ships a sync orchestrator (`/api/sync/negotiate`) that, given your local save manifest + a device id, returns a per-save operation plan (`upload` / `download` / `conflict` / `no_op`). So we don't reimplement conflict logic — we just submit a manifest and execute the plan. This is exactly what grout (the Go client) does; the client is a thin scanner + plan executor + UI.

Adding it as a **separate package** (`savesync/`) keeps the existing downloader untouched. Nothing in `romm_drop.py` changes behavior unless the new sync screen is entered.

## 2. RomM API contract (verified against grout `romm/*.go`)

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/devices` | POST | Register device → `{device_id}` (also `GET /api/devices`, `GET /api/devices/{id}`) |
| `/api/sync/negotiate` | POST | Send `{device_id, saves[]}` manifest → `{session_id, operations[], totals}` |
| `/api/sync/sessions/{session_id}/complete` | POST | Finish session with `{operations_completed, operations_failed}` |
| `/api/saves` | GET | List saves (`rom_id`, `device_id`, `slot`, `emulator`, `platform_id`) |
| `/api/saves` | POST | Upload new save (multipart `saveFile`, `UploadSaveQuery`) |
| `/api/saves/{id}` | PUT | Re-upload/overwrite existing save (multipart) |
| `/api/saves/{id}/content` | GET | Download save bytes. **Send `optimistic=false` + `device_id`** |
| `/api/saves/{id}/downloaded` | POST | Confirm local write  marks device synced. **`device_id` goes in the JSON body, not the query string** (query is rejected with 422) |
| `/api/saves/summary` | GET | `{total_count, slots[{slot,count,latest}]}` per ROM (rom_id query) |

**Request bodies** (negotiate):
```json
// POST /api/sync/negotiate
{
  "device_id": "uuid",
  "saves": [
    {
      "rom_id": 12,
      "file_name": "zelda.srm",
      "slot": "autosave",          // omit for autosave; named slots for savestates
      "emulator": "retroarch",
      "content_hash": "sha256hex",
      "updated_at": "2026-08-05T10:00:00Z",
      "file_size_bytes": 4096
    }
  ]
}
// response.operations[] each:
{ "action": "upload|download|conflict|no_op", "rom_id":.., "save_id":..?,
  "file_name":.., "slot":..?, "emulator":.., "reason":..,
  "server_updated_at":..?, "server_content_hash":..? }
```

`UploadSaveQuery`: `rom_id, device_id, slot, emulator, overwrite, autocleanup, autocleanup_limit`.

**Auth (verified live on RomM 5.1.0):** every API endpoint requires a `Authorization: Bearer <token>` header. Basic-auth with an empty username (which older RommDrop docs suggested) is rejected with **403** on `/api/platforms`, `/api/roms`, `/api/saves`, `/api/devices` and **403** on `/api/sync/negotiate` too. The client sends the Bearer header on every request; no `auth=` tuple needed.

**Pagination (`GET /api/roms`):** response is `{items[], total, offset, limit, ...}`  page with `platform_ids` (plural), `limit`, `offset` until `offset >= total`. Truncating the first page silently drops ROMs from the platform map, which breaks download destination resolution.

## 3. Data flow

```
1 SCAN    find local save/state files per platform (scanner + save_directories.json)
2 MATCH   map each local file -> rom_id  (resolve via platform ROM list + filename)
3 NEGOTIATE  POST /sync/negotiate {device_id, saves[]}  -> plan
4 EXECUTE    for each op: upload (POST/PUT /saves), download (GET .../content,
             write file, then POST .../downloaded), conflict (policy, see §6)
5 COMPLETE   POST /sync/sessions/{id}/complete
6 REPORT     screen summary: N uploaded, M downloaded, K conflicts, L no-op
```

## 4. Conflict handling (Phase 3  IMPLEMENTED)

### Reality check (live-verified)
The RomM 5.1 server does **not** return a raw `conflict` action during
normal negotiation. It resolves every divergent save to a concrete action
(`upload` / `download` / `no_op`) with a `reason`, picking newest-wins by
`updated_at`:
- client newer than last sync  `upload` ("Client save is newer than last sync")
- client older  `download`
- identical  `no_op` ("No changes since last sync")

A `conflict` op therefore only appears in genuinely ambiguous ties and is a
**defensive fallback**: the engine handles it rather than assuming the server
never sends it.

### Engine policies (all four, offline-tested)
`engine._resolve_conflict(op, policy, allow_upload, cfg)` selects per game:

| Policy | Decision |
|---|---|
| Keep local | upload with `overwrite=true` |
| Take server | download, replace local (`allow_upload` irrelevant) |
| Auto | **newest `updated_at` wins**: server newer  download; local newer  upload |
| Skip | leave both untouched |

- `engine.preview_conflicts(plan, policy)` is pure (no disk/network)  returns
  `[(op, "upload"|"download"|"skip")]` for the UI to render before commit.
- Policy is **validated up front** in `run()`  unknown policy raises
  `ValueError` instead of being swallowed as a per-op failure.
- Rationale: on the real server the divergent-save case lands as `upload`/`download`
  and is handled by the same execution path; the `conflict` branch is only for
  defensive completeness.

### Key contract facts that made keep-local/auto work
- `_local_for` matches the scanned local save by **rom_id + slot** using
  tag-normalized stems (the op's `file_name` carries a `[timestamp]` tag and
  never equals the clean local filename). `rom_id` on the scanned `LocalSave`
  is mandatory.
- `upload_save(local, device_id, overwrite)` now takes `device_id` explicitly
  (the old module-level `_active_device_id`/`_require_device()` global was
  never populated  uploads would have raised "no active device_id set").
- `resolved_as` is stamped on each conflict op by the engine for post-mortem /
  UI reporting.

### Tests
`savesync/tests/test_conflicts.py` (no network, fake client + real temp files):
download apply+confirm, upload apply, allow_upload gating, all four policies,
`auto` newest-wins both directions, `preview_conflicts`, unknown-policy raise.
Total integrity (downloads byte-verified, uploads use real local file).

Phase 2 (download-only), Phase 3 (conflicts), and the CLI + pygame GUI
wiring are done. Open item: confirm the RetroBat save-dir layout on the
Windows box so the shipped `save_directories.json` skeleton is corrected to
the real install.

## 5. The genuinely non-trivial parts

1. **Device registration & pairing.** Device-aware saves require a registered device and RomM 5.0+. RommDrop uses basic auth (username/password). For personal use this usually suffices to reach `/api/devices` + `/api/sync/*`; if your RomM forces device-pairing auth, we must add the `/api/auth/device/init` + `/api/auth/device/token` flow. **Verify against your server in Phase 1.**
2. **Save directory discovery.** grout ships per-CFW `save_directories.json`. RetroBat stores saves in emulator-specific locations (RetroArch core saves, savestates, standalone emulators). This is a mapping of `platform_fs_slug` → local folder(s), and it's install-specific. We ship a sensible skeleton + a READ-ONLY "discover what I actually have" pass so the user can correct the map without guessing.
3. **Save vs savestate.** Saves (`.srm`/`.sav`) and savestates (`.state`/slot files) both sync, but savestates are **only compatible between identical emulator binaries**. We sync savestates under named `slot`s and surface an in-app warning, mirroring grout's guidance.
4. **Hash stability.** Store SHA-256 of file content for `content_hash`. Cheap enough for save files (KB–MB).

## 5. Package layout (scaffolded)

```
RommDrop/
  romm_drop.py            # pygame frontend: SYNC screen drives SyncSession
  savesync/               # self-contained, pygame-free
    __init__.py           # public exports
    models.py             # dataclasses: Save, ClientSaveState, SyncPlan, Operation, ...
    api.py                # RomMClient: CRM requests for /devices, /saves, /sync
    config.py             # load config.json + device handling + paths
    scanner.py            # local save discovery: map fs_slug -> save dirs -> files -> hash
    engine.py             # orchestrator: scan/match/conflict resolution (preview_conflicts)
    pipeline.py           # scan_negotiate() + execute() + SyncSession (shared by CLI + GUI)
    cli.py                # argparse frontend (register / plan / sync) + discover_creds/ensure_device
    save_directories.json # fs_slug -> save/state subdir + extensions (skeleton, editable)
    tests/                # 40 tests: pipeline, conflicts, cli, gui (headless SDL), boot
  docs/
    save-sync/INTEGRATION.md   # how the GUI + CLI share the pipeline
```

## 6. Integration points in `romm_drop.py` (outlined in INTEGRATION.md)

- Add a ` [ SAVE SYNC ]` action item at the top of the platform list (`item["type"]="SYNC_MODE"`).
- New GUI state `"SYNC"`: `enter_sync()` spawns a read-only plan worker; a
  pygame-free `SyncSession` holds all state; `refresh_sync_items()` renders
  the device summary, per-conflict policy preview, policy/upload controls, and
  run action. `run_sync()` executes on a thread. Progress bar reuses the
  existing `is_downloading` slot.
- Controller: X cycles policy, Y toggles the upload gate, A runs, B backs out.
- Reuse `COLORS`, fonts, `status_msg`, and the `draw()` footer. No fundamental UI surgery.

## 7. Build phases

| Phase | Scope | Risk to existing data |
|---|---|---|
| **1** | Read-only audit: discover save dirs, match `rom_id`, call `negotiate`, render plan (no transfers) | None (pure reads) |
| **2** | Execute `download` ops only; uploads behind an explicit flag; `conflict`/`no_op` logged | Low (overwrites only where server wins intentionally) |
| **3** | Full bidirectional: uploads, overwrite, savestate slots, conflict resolution in UI | Medium — resolved explicitly via UI |

Phase gates so `conflict` handling and uploads are never silent.

## 8. Open questions (need user/server answers in Phase 1)

1. Your RomM server version ( 5.0?) and auth mode (basic vs device-pairing).
2. RetroBat root layout + where each emulator keeps `.srm`/`.sav`/`.state`.
3. Which systems must support savestates (extra slot semantics), vs saves only for now.
4. Sync trigger model: manual (from the UI) vs auto-on-launch prompt.

## 9. Phase 1 verified against live server (2026-08-05)

Server: RomM 5.1.0 at romm.claassen.family. All facts below confirmed by
live API calls; nothing was written (test sessions closed with 0 ops).

- **Auth**: personal token `rmm_...` works as `Authorization: Bearer` AND as
  basic-auth password with empty username (`auth=("", token)`), so the
  existing RommDrop basic-auth plumbing needs no change.
- **Devices**: `POST /api/devices` registers a client
  (`{name, platform, client, client_version, sync_mode}`) and returns
  `device_id` (201). Server already has the user's R36S running grout
  v5.0.0.0; RommDrop registered as its own device (separate save origin).
- **Save slots**: server saves live under `slot: "autosave"` for battery
  saves; savestates use the slot digit (`.state1` -> `"1"`). Client manifest
  MUST send the same slot, else the server treats it as a different save and
  schedules a spurious upload. `scanner.slot_from_ext` implements this.
- **Rom object field**: RomM 5.1 roms use `fs_name` (e.g.
  `Wario Land 3 (World) (En,Ja).zip`), NOT `file_name`. Server-side saves add
  a `[upload-timestamp]` tag to the filename. `scanner.match_local_to_rom`
  normalizes both sides (strip `[...]`/`(...)` tags, lowercase, strip
  non-alnum) before comparing stems.
- **Negotiate contract**: `POST /api/sync/negotiate` with
  `{device_id, saves: [ClientSaveState...]}` returns `{session_id, operations,
  totals}`. Operations are `upload`/`download`/`conflict`/`no_op` with a
  `reason`. The server answers with a FULL reconciliation: ops for every
  server save the client lacks, not just the submitted ones.
- **Decision example**: a fresh device with a save identical to the server's
  was planned as `upload` ("Client save is newer than last sync") plus
  `download` for the other 3 server saves missing locally  correct first-sync
  behavior for a new device.
- **Complete**: `POST /api/sync/sessions/{id}/complete` with
  `{operations_completed, operations_failed}` returns status `COMPLETED`.
  An abandoned session should be completed with 0/0 to avoid dangling state.

Real-data smoke test (scanner -> matcher -> negotiate -> complete) passes
end-to-end. Phase 1 is done; remaining open question is the RetroBat save-dir
layout on the Windows box and the sync trigger UX (Phase 1 item 4).