"""Headless smoke test for the RommDrop GUI save-sync screen.

Runs with SDL_VIDEODRIVER=dummy so no display, controller, or network is
needed. Covers the SYNC state wiring in romm_drop.py:
  - enter_sync() builds the planning rows and spawns the plan worker
  - refresh_sync_items() renders session summary + conflict preview
  - cycle_sync_policy() / toggle_upload_gate() drive the pygame-free session
  - run_sync() drives execute and surfaces the result
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import pytest

pygame = pytest.importorskip("pygame")

import romm_drop  # noqa: E402  (needs the dummy videodriver before import)


class StubEngine:
    def preview_conflicts(self, plan, policy):
        return []


class StubPlan:
    session_id = 123
    total_upload = total_download = total_conflict = total_no_op = 0


class StubResult:
    uploaded = downloaded = 0
    failed = []


class StubSession:
    def __init__(self):
        self.phase = "ready"
        self.policy = "auto"
        self.allow_upload = False
        self.result = None
        self.error = ""
        self.progress = (0, 0)
        self.plan = StubPlan()
        self.engine = StubEngine()
        self._preview = []

    def scan(self):
        self._preview = [("Game.srm", "download")]

    @property
    def summary(self):
        return f"{self.plan.total_upload} up / {self.plan.total_download} down"

    @property
    def preview_lines(self):
        return self._preview

    def set_policy(self, policy):
        self.policy = policy
        self._preview = [("Game.srm", {"keep_local": "upload",
                                       "take_server": "download"}.get(policy, "skip"))]

    def toggle_upload(self):
        self.allow_upload = not self.allow_upload
        return self.allow_upload

    def execute(self, **kw):
        self.phase = "done"
        self.result = StubResult()


def _make_gui(monkeypatch):
    # prevent the real __init__ from hitting the network / opening a window
    gui = romm_drop.RommDropGUI.__new__(romm_drop.RommDropGUI)
    gui.state = "PLATFORMS"
    gui.search_focus = "keyboard"
    gui.cached_platforms = []
    gui.items = []
    gui.selected_index = 0
    gui.scroll_offset = 0
    gui.query = ""
    gui.status_msg = ""
    gui.is_downloading = False
    gui.is_syncing = False
    gui.progress = 0
    gui.sync_session = None
    gui.list_item_rects = []
    gui.kb_key_rects = []
    return gui


def test_enter_sync_builds_rows(monkeypatch):
    gui = _make_gui(monkeypatch)
    # stub the worker thread so no network/thread runs
    monkeypatch.setattr(romm_drop.threading.Thread, "start",
                        lambda self: None)
    gui.enter_sync()
    assert gui.state == "SYNC"
    names = [i["name"] for i in gui.items]
    assert any("Planning" in n for n in names)
    assert any(n == "cd.. [ Back to Systems ]" for n in names)


def test_refresh_renders_summary_and_policy(monkeypatch):
    gui = _make_gui(monkeypatch)
    gui.sync_session = StubSession()
    gui.sync_session.scan()
    gui.refresh_sync_items()
    names = [i["name"] for i in gui.items]
    assert any("Game.srm" in n for n in names)            # conflict preview
    assert any("Policy: auto" in n for n in names)
    assert any("Allow uploads: OFF" in n for n in names)
    types = {i["type"] for i in gui.items}
    assert {"SYNC_RUN", "SYNC_POLICY", "SYNC_UPLOAD"} <= types


def test_cycle_policy_and_upload_gate(monkeypatch):
    gui = _make_gui(monkeypatch)
    gui.sync_session = StubSession()
    gui.cycle_sync_policy()
    assert gui.sync_session.policy == "keep_local"
    gui.cycle_sync_policy()
    gui.cycle_sync_policy()
    assert gui.sync_session.policy == "skip"
    gui.toggle_upload_gate()
    assert gui.sync_session.allow_upload is True
    # the upload row reflects the new gate
    names = [i["name"] for i in gui.items]
    assert any("Allow uploads: ON" in n for n in names)


def test_run_sync_marks_done(monkeypatch):
    gui = _make_gui(monkeypatch)
    gui.sync_session = StubSession()
    monkeypatch.setattr(romm_drop.threading.Thread, "start",
                        lambda self: gui.sync_run_worker())
    gui.run_sync()
    assert gui.sync_session.phase == "done"
    names = [i["name"] for i in gui.items]
    assert any(n.startswith("Done:") for n in names)