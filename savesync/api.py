"""RomM API client for the save-sync extension.

Reuses the same auth style as `romm_drop.py` (requests + basic auth from
config.json). All endpoints are RomM 5.0+ device-aware save/sync APIs.
"""

from __future__ import annotations

from pathlib import Path

import requests

from .models import LocalSave, ServerSave, SyncOperation, SyncPlan

ENDPOINT_DEVICES = "/api/devices"
ENDPOINT_PLATFORMS = "/api/platforms"
ENDPOINT_ROMS = "/api/roms"
ENDPOINT_SYNC_NEGOTIATE = "/api/sync/negotiate"
ENDPOINT_SYNC_COMPLETE = "/api/sync/sessions/{session_id}/complete"
ENDPOINT_SAVES = "/api/saves"
ENDPOINT_SAVE_BY_ID = "/api/saves/{save_id}"
ENDPOINT_SAVE_CONTENT = "/api/saves/{save_id}/content"
ENDPOINT_SAVE_DOWNLOADED = "/api/saves/{save_id}/downloaded"
ENDPOINT_SAVE_SUMMARY = "/api/saves/summary"


class RomMError(RuntimeError):
    """Raised on non-2xx RomM API responses."""


class RomMClient:
    def __init__(self, base_url: str, auth: tuple[str, str], timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.timeout = timeout
        # RomM 5.x requires a Bearer token header on every API endpoint
        # (basic-auth is only accepted by some legacy paths, if any).
        self.headers = {
            "User-Agent": "RommDrop/0.3+savesync",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        if auth and auth[1]:
            self.headers["Authorization"] = f"Bearer {auth[1]}"

    # ------------------------------------------------------------------ devices

    def register_device(
        self,
        name: str,
        platform: str = "windows",
        client: str = "rommdrop",
        client_version: str = "0.3",
    ) -> str:
        """POST /api/devices -> returns the server-assigned device_id."""
        body = {
            "name": name,
            "platform": platform,
            "client": client,
            "client_version": client_version,
            "sync_mode": "api",
        }
        r = requests.post(
            f"{self.base_url}{ENDPOINT_DEVICES}",
            json=body,
            headers=self.headers,
            timeout=self.timeout,
        )
        self._check(r)
        data = r.json()
        return data.get("device_id") or data.get("id")

    def get_device(self, device_id: str) -> dict:
        r = requests.get(
            f"{self.base_url}{ENDPOINT_DEVICES}/{device_id}",
            headers=self._bearer_headers(),
            timeout=self.timeout,
        )
        self._check(r)
        return r.json()

    def _bearer_headers(self) -> dict:
        """Alias for self.headers (Bearer auth is set in __init__)."""
        return self.headers

    def list_platforms(self) -> list[dict]:
        """GET /api/platforms -> [{id, slug, fs_slug, name, ...}]."""
        r = requests.get(f"{self.base_url}{ENDPOINT_PLATFORMS}",
                         headers=self._bearer_headers(), timeout=self.timeout)
        self._check(r)
        return r.json()

    def list_roms(self, platform_id: int, limit: int = 500) -> list[dict]:
        """GET /api/roms for one platform (paginated; `platform_ids` filter)."""
        out, offset = [], 0
        while True:
            params = {"platform_ids": platform_id, "limit": limit, "offset": offset}
            r = requests.get(f"{self.base_url}{ENDPOINT_ROMS}", params=params,
                             headers=self._bearer_headers(), timeout=self.timeout)
            self._check(r)
            data = r.json()
            batch = data.get("items", [])
            out.extend(batch)
            total = int(data.get("total") or len(batch))
            offset += len(batch)
            if not batch or offset >= total:
                break
        return out

    def search_roms(self, platform_id: int, search_term: str,
                    limit: int = 100) -> list[dict]:
        """Targeted ROM lookup by name, avoiding a full platform listing.

        Local saves only need their own ROMs resolved (for the manifest and
        download paths), so we query per stem instead of paging through every
        ROM on the platform (a 1500-ROM platform takes ~20s to list but
        ~0.15s per targeted search).
        """
        params = {
            "platform_ids": platform_id,
            "search_term": search_term,
            "limit": limit,
            "offset": 0,
        }
        r = requests.get(f"{self.base_url}{ENDPOINT_ROMS}", params=params,
                         headers=self._bearer_headers(), timeout=self.timeout)
        self._check(r)
        return r.json().get("items", [])

    def build_platform_map(self) -> dict[int, str]:
        """rom_id -> platform fs_slug (for resolving download destinations)."""
        m: dict[int, str] = {}
        for plat in self.list_platforms():
            for rom in self.list_roms(plat["id"]):
                m[int(rom["id"])] = plat.get("fs_slug") or plat.get("slug")
        return m

    # ------------------------------------------------------------------- saves

    def list_saves(self, rom_id: int | None = None, device_id: str | None = None,
                   slot: str | None = None) -> list[ServerSave]:
        """GET /api/saves with optional rom_id / device_id / slot filters."""
        params = {}
        if rom_id:
            params["rom_id"] = rom_id
        if device_id:
            params["device_id"] = device_id
        if slot:
            params["slot"] = slot
        r = requests.get(
            f"{self.base_url}{ENDPOINT_SAVES}",
            params=params,
            headers=self._bearer_headers(),
            timeout=self.timeout,
        )
        self._check(r)
        return [ServerSave.from_json(s) for s in r.json()]

    def upload_save(self, local: LocalSave, device_id: str,
                    overwrite: bool = False) -> ServerSave:
        """POST /api/saves (multipart saveFile).

        Requires local.rom_id to be resolved. Returns the created save record.
        """
        if not local.rom_id:
            raise RomMError("upload_save requires a resolved rom_id")
        if not local.file_path or not Path(local.file_path).exists():
            raise RomMError(
                "upload_save requires a real local file (file_path missing): "
                f"{local.file_name}"
            )
        params = {
            "rom_id": local.rom_id,
            "device_id": device_id,
            "slot": local.slot or "autosave",
            "emulator": local.emulator,
            "overwrite": overwrite,
        }
        with open(local.file_path, "rb") as f:
            files = {"saveFile": (local.file_name, f, "application/octet-stream")}
            r = requests.post(
                f"{self.base_url}{ENDPOINT_SAVES}",
                params=params,
                files=files,
                    headers=self.headers,
                timeout=self.timeout,
            )
        self._check(r)
        return ServerSave.from_json(r.json())

    def download_save(self, op: SyncOperation, dest_path: str,
                      device_id: str, optimistic: bool = False) -> None:
        """GET /api/saves/{id}/content?device_id=..&optimistic=..

        optimistic must stay False until the file is fully written and
        `confirm_download` is called  otherwise the server marks the device
        synced before the file actually exists locally.
        """
        if not op.save_id:
            raise RomMError("download_save requires op.save_id")
        params = {"device_id": device_id, "optimistic": optimistic}
        r = requests.get(
            f"{self.base_url}{ENDPOINT_SAVE_CONTENT.format(save_id=op.save_id)}",
            params=params,
            headers=self.headers,
            timeout=self.timeout,
            stream=True,
        )
        self._check(r)
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)

    def confirm_download(self, save_id: int, device_id: str) -> None:
        """POST /api/saves/{id}/downloaded  marks device as synced for this save.

        `device_id` goes in the JSON body (query param is rejected with 422).
        """
        r = requests.post(
            f"{self.base_url}{ENDPOINT_SAVE_DOWNLOADED.format(save_id=save_id)}",
            json={"device_id": device_id},
            headers=self.headers,
            timeout=self.timeout,
        )
        self._check(r)

    # -------------------------------------------------------------------- sync

    def negotiate(self, device_id: str, local_saves: list[LocalSave]) -> SyncPlan:
        """POST /api/sync/negotiate with the local manifest."""
        body = {
            "device_id": device_id,
            "saves": [s.to_manifest() for s in local_saves if s.rom_id],
        }
        r = requests.post(
            f"{self.base_url}{ENDPOINT_SYNC_NEGOTIATE}",
            json=body,
            headers=self.headers,
            timeout=self.timeout,
        )
        self._check(r)
        return SyncPlan.from_json(r.json())

    def complete_session(self, session_id: int, completed: int, failed: int) -> None:
        """POST /api/sync/sessions/{session_id}/complete."""
        body = {"operations_completed": completed, "operations_failed": failed}
        r = requests.post(
            f"{self.base_url}{ENDPOINT_SYNC_COMPLETE.format(session_id=session_id)}",
            json=body,
            headers=self.headers,
            timeout=self.timeout,
        )
        self._check(r)

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _check(r: requests.Response) -> None:
        if r.status_code >= 400:
            raise RomMError(f"{r.request.method} {r.url} -> {r.status_code}: {r.text[:200]}")
