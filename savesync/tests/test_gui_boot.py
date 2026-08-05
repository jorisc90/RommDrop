"""Headless boot test: construct the REAL RommDropGUI and pump a few frames.

Monkeypatches only the network-touching fetch_platforms() so nothing hits the
wire. Proves the real __init__ (pygame init, fullscreen set_mode, fonts,
joystick probing) holds up under a dummy SDL video driver, and that run()
processes a QUIT event cleanly.
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

import romm_drop  # noqa: E402  (needs dummy videodriver before import)


def test_real_constructor_boots_and_quits(monkeypatch):
    # No network: platforms come from a stub.
    monkeypatch.setattr(romm_drop.RommDropGUI, "fetch_platforms",
                        lambda self: setattr(self, "cached_platforms", [
                            {"name": " [ SEARCH / MANUAL ENTRY ]", "type": "SEARCH_MODE"},
                        ]))

    gui = romm_drop.RommDropGUI()
    assert gui.screen is not None
    assert gui.screen_width > 0 and gui.screen_height > 0
    assert gui.state == "PLATFORMS"
    assert gui.cached_platforms[0]["type"] == "SEARCH_MODE"
    assert gui.sync_session is None and gui.is_syncing is False

    # The platform list is drawable under the dummy driver.
    gui.draw()
    pygame.display.flip()

    # A QUIT event must terminate the loop after at least one full pump.
    pygame.event.post(pygame.event.Event(pygame.QUIT))
    gui.run()
    assert gui.running is False