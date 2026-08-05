# Save Sync  implementation guide (Phase 5: GUI wired)

**Status:** DONE. The CLI was built and live-verified against RomM 5.1.0 first
(`savesync/cli.py`); the pygame GUI screen in `romm_drop.py` now drives the
same code path through a pygame-free `SyncSession`. 40 tests green, including
a headless SDL boot test.

Architecture: **one pipeline, two frontends.**

```
savesync/pipeline.py      scan_negotiate() + execute() + SyncSession (no pygame)
savesync/api.py           RomMClient (devices, negotiate, upload, download)
savesync/engine.py        scan/match/conflict resolution (preview_conflicts)
savesync/cli.py           argparse frontend; discover_creds(), ensure_device(), load_cfg()
romm_drop.py              pygame frontend: SYNC state renders the session
```

## 1. Credentials / device state (shared)

- `discover_creds()` (cli.py): `--url/--token` flags > `ROMM_URL`/`ROMM_TOKEN`
  env > `~/.hermes/secrets.json` (`romm.base_url`/`romm.rmm_token`) >
  `config.json` (`romm_url` + password-as-token). Never rewrites `config.json`.
- Device id persists to `savesync_state.json` (`STATE_PATH`, repo root).
- `ensure_device(client, cfg, state_path)` verifies the persisted id, or
  registers a fresh device  call it before `scan_negotiate`.

## 2. The pipeline (`savesync/pipeline.py`)

```python
engine, plan = scan_negotiate(client, cfg, RETROBAT_ROOT)   # read-only
session = SyncSession(engine, plan, client)                 # pygame-free state
session.scan()          # build conflict preview under current policy
session.set_policy(p)   # auto | keep_local | take_server | skip
session.toggle_upload() # gates uploads (OFF by default  safety)
session.execute()       # applies the plan; populates .result/.progress
```

- `plan` is a `SyncPlan` (session_id, total_upload/download/conflict/no_op,
  per-op list). `engine.preview_conflicts(plan, policy)` returns
  `[(file_name, resolved)]` for the GUI's preview rows.
- `execute()` runs the negotiated ops (downloads + optionally uploads),
  updates `(done, total)` progress, and completes the server session so no
  dangling sessions are left (same behaviour as CLI `sync --dry-run`).
- `SyncSession` carries `phase` (`ready|running|done|error`), `error`,
  `result` (`uploaded`/`downloaded`/`failed`). The GUI is a pure renderer of
  this object  no pygame knowledge leaks into `savesync/`.

## 3. GUI wiring (`romm_drop.py`)

No `# [SAVESYNC]` tags are used  the sync screen is a permanent feature now.

- Platform list: `fetch_platforms()` prepends
  `{"name": " [ SAVE SYNC ]", "type": "SYNC_MODE"}`.
- `handle_selection()` routes `SYNC_MODE` -> `enter_sync()`; while
  `self.state == "SYNC"`, `SYNC_RUN`/`SYNC_POLICY`/`SYNC_UPLOAD` rows call
  `run_sync()`/`cycle_sync_policy()`/`toggle_upload_gate()`.
- `enter_sync()` spawns `sync_plan_worker` (thread): `discover_creds` ->
  `RomMClient` -> `load_cfg` -> `ensure_device` -> `scan_negotiate` ->
  `SyncSession.scan()`. Errors surface in `status_msg`; the app never crashes.
- `refresh_sync_items()` rebuilds rows from the session: device + summary
  header, per-conflict preview lines, `Policy: (X)`, `Allow uploads: (Y)`,
  `Run sync (A)` (or `Done: x up / y down / z failed`).
- `run_sync()` -> `sync_run_worker` (thread) calls `session.execute()`.
- Controller: X cycles policy, Y toggles the upload gate (SYNC state only).
  A/B are the standard accept/back buttons  A runs, B returns to platforms.
- Progress: while `is_downloading` or `(is_syncing and phase == "running")`,
  the bottom bar fills with `sync_session.progress[0]/progress[1]`.

## 4. Phase gates (safety)

| Phase | `allow_upload` | Conflicts |
|---|---|---|
| plan / preview | OFF (default) | previewed per policy |
| execute | OFF | resolved by policy (no uploads) |
| full sync | ON (user toggles Y) | resolved by policy |

Until the user turns uploads ON, the server can never be overwritten  the
GUI starts every session with `allow_upload=False`.

## 5. Tests

```bash
cd RommDrop
python3 -m pytest savesync/tests -q          # 40 passed
```

- `test_pipeline.py`  scan/negotiate read-only purity, platform scoping,
  execute + upload gating, per-op progress, SyncSession policy cycle/toggle.
- `test_cli.py`  command surface, cred discovery order, dry-run, output.
- `test_gui.py`  SYNC screen rendering, policy cycling, upload gate, run-done
  (SDL dummy driver, no display/controller/network needed).
- `test_gui_boot.py`  real `RommDropGUI.__init__` + `run()` under
  `SDL_VIDEODRIVER=dummy`: constructor, draw, clean QUIT shutdown.

Requires `pygame` in the venv (`python3 -m pip install pygame`) for the two
GUI test files; they `importorskip` if absent so the rest still run.

## 6. CLI (still the fastest way to test end-to-end)

```bash
python3 savesync/cli.py register                    # verify or register device
python3 savesync/cli.py plan --root RETROBAT_ROOT   # read-only plan + preview
python3 savesync/cli.py sync --root RETROBAT_ROOT --allow-upload
# flags: --url --token --config --platform gbc --policy auto|keep_local|take_server|skip --dry-run --debug
```

## 7. Rollback

`savesync/` is self-contained and removable; `romm_drop.py` sync code lives
under the `# ---- save sync ----` banner (delete that block + the
`SYNC_MODE` platform entry + the SYNC branches in `handle_selection` /
controller X/Y handling to revert to a plain downloader).
