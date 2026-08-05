# RommDrop

RommDrop is a dead simple ROM downloader for RetroBat and EmulationStation. It provides a lightweight, controller-driven interface to browse and download ROMs from a RomM server directly into the appropriate local platform directories. RommDrop is designed to be self-contained. It uses a portable Python interpreter within its own directory structure, ensuring that no system-wide Python installation is required for the end user.


***Features***

- Automatically detects and lists platforms available on the RomM server, loads this on launch

- Includes a search function an on-screen keyboard, unfortunately real keyboard access is not currently supported..

- Detects the RetroBat root directory and places downloaded files in the correct subfolders based on platform slugs. "It Just Works."

- U.I. Built with Pygame for easy borderless fullscreen and native controller input.


***Setup and Installation***

- Place the "roms" folder found in RommDrop_current in your RetroBat Base Folder, something like C:/Games/RetroBat

- The application requires a config.json file located in the .RommDrop directory. Edit this file with your server details:

***Config JSON example***

{
    "romm_url": "https:// or http://yourserver:6969",
    "username": "YourUsername",
    "password": "YourPassword"
}

***Authentication***

RomM uses an API key to authenticate. You can authenticate two ways:

1. **Password / API key** (recommended for RomM) — put your key in the `password`
   field and leave `username` empty. RomMDrop sends it as a `Bearer` token, which
   is what current RomM servers expect:

   {
       "romm_url": "https://romm.example.com",
       "username": "",
       "password": "rmm_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
   }

2. **Username + password (basic auth)** — only works on legacy RomM setups that
   still accept basic auth:

   {
       "romm_url": "https://romm.example.com",
       "username": "YourUsername",
       "password": "YourPassword"
   }

Notes:
- If `password` looks like an API key (starts with `rmm_`), RomMDrop automatically
  sends it as a `Bearer` token to `/api`. Otherwise it falls back to HTTP basic
  auth. For reliable access on RomM 5.x you should use a token in the `password`
  field.
- Find your token in RomM under Settings -> API Keys. Generate one and paste it
  into the `password` field.
- Keep config.json out of version control (it is already covered by .gitignore)
  so you never commit a live token.
- The server URL must not include a trailing `/api` — RomMDrop appends it for you.


***Navigation***

The interface is designed for 100% controller navigation. There is unfortunately no way to navigate the app with a mouse and keyboard currently.

- D-Pad: Navigate menus and on-screen keyboard.

- A Button: Select platform, download game, or type character.

- B Button: Universal back button to return to the system list.

- LB / RB: Page navigation (scroll through lists 10 items at a time).

- Start + Select: Exit the application and return to the frontend.

- Y Button: Instant jump to Search Mode.

- X Button: Backspace (delete last character) while typing.


***Command Line Interface (save sync)***

RommDrop bundles a save/state sync CLI (`savesync/cli.py`) that runs the full
scan -> match -> negotiate -> execute pipeline from the terminal (no pygame
needed). Run it as a module so the package imports resolve:

    python -m savesync.cli <command> [options]

Positional commands:

- register — verify or register the sync device with the RomM server
- plan     — scan + negotiate only (read-only, no downloads/uploads or writes)
- sync     — scan, negotiate and execute (uploads enabled by default)

Global options (placed before the command):

- `--url URL`        RomM base URL (overrides env/config)
- `--token TOKEN`    RomM API token (overrides env/config)
- `--config CONFIG`  path to config.json (default: RommDrop/config.json)
- `--root ROOT`      save root dir (default: current directory)
- `--platform PLATFORM`  only sync one platform fs_slug, e.g. gbc
- `--debug`          verbose engine output

sync-only options:

- `--no-upload`      disable uploads (default: uploads enabled)
- `--policy {auto,keep_local,skip,take_server}`  conflict policy (default: auto)
- `--dry-run`        negotiate + preview, do not execute

Examples:

    python -m savesync.cli register --url https://romm.example.com --token rmm_xxx
    python -m savesync.cli plan --root C:/RetroBat
    python -m savesync.cli sync --root C:/RetroBat
    python -m savesync.cli sync --root C:/RetroBat --no-upload --policy skip

Conflict policies:

- `auto` — newest `updated_at` wins; ties keep the local file (default)
- `keep_local` — push the local file up
- `take_server` — pull the server file down
- `skip` — do nothing on conflict

Note: the `--url`/`--token` flags are optional if the same details are already
available via the environment (`ROMM_URL` / `ROMM_TOKEN`), `~/.hermes/secrets.json`,
or `config.json` (see Authentication above).
