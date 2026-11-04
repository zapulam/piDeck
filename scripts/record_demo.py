"""Record the README walkthrough using isolated mock data and the actual browser UI.

Install requirements-dev.txt, Pillow, and Playwright Chromium, then run:
    python scripts/record_demo.py
No real accounts, user settings, session transcripts, or desktop profile are used.
"""
from __future__ import annotations

import argparse
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

import httpx
from PIL import Image
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pideck.desktop import local_server
from pideck.sources import mock

WIDTH, HEIGHT, FPS = 800, 480, 10


"""# --- Mock source and recording helpers ---"""


async def seed_demo(
        hub,
        agents,
        feed,
        lively=True,
):
    """Seed stable demo data without background events interrupting the recording.

    Args:
        hub: Temporary server's state and websocket hub.
        agents: Temporary session registry.
        feed: Temporary Slack feed.
        lively: Unused compatibility argument accepted by the normal demo runner.
    """
    await mock.seed(hub, agents, feed)


def capture(
        page,
        frames,
        seconds,
):
    """Capture a scene at up to ten frames per second, retaining its actual timing.

    Args:
        page: Isolated Playwright page displaying the application.
        frames: Mutable sequence of timestamp and RGB image pairs.
        seconds: Minimum scene duration in seconds.
    """
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        started = time.monotonic()
        png = page.screenshot()
        frames.append((started, Image.open(BytesIO(png)).convert("RGB")))
        page.wait_for_timeout(max(0, (1 / FPS - (time.monotonic() - started)) * 1000))


def encode_gif(
        frames,
        destination,
):
    """Encode captured frames with a shared palette and verify the resulting GIF.

    Args:
        frames: Timestamp and RGB image pairs from the browser recording.
        destination: Output GIF path, normally docs/demo.gif.

    Raises:
        ValueError: The recording is empty, invalid, or too large for the README.
    """
    if not frames:
        raise ValueError("No demo frames were captured")
    samples = frames[::max(1, len(frames) // 16)]
    sheet = Image.new("RGB", (WIDTH, HEIGHT * len(samples)))
    for index, (_, frame) in enumerate(samples):
        sheet.paste(frame, (0, index * HEIGHT))
    palette = sheet.quantize(colors=192, method=Image.Quantize.MEDIANCUT)
    images = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for _, frame in frames]
    durations = [max(20, round((b[0] - a[0]) * 100) * 10) for a, b in zip(frames, frames[1:])]
    durations.append(100)
    destination.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(destination, save_all=True, append_images=images[1:], duration=durations,
                   loop=0, optimize=True, disposal=1)
    with Image.open(destination) as result:
        duration = sum(result.seek(i) or result.info.get("duration", 0) for i in range(result.n_frames))
        if result.size != (WIDTH, HEIGHT) or result.n_frames < 2 or destination.stat().st_size > 10_000_000:
            raise ValueError("The demo GIF failed size, frame-count, or file-size validation")
        print(json.dumps({"path": str(destination), "frames": result.n_frames,
                          "seconds": duration / 1000, "bytes": destination.stat().st_size}))


"""# --- Walkthrough scenes ---"""


def record(
        destination,
):
    """Record provider groups, animations, completion acknowledgment, and other pages.

    Args:
        destination: GIF output path.

    Raises:
        AssertionError: A browser error occurs or a demonstrated feature is missing.
    """
    frames, errors = [], []
    with tempfile.TemporaryDirectory(prefix="pideck-demo-") as folder:
        config = Path(folder) / "config.yaml"
        config.write_text("alerts:\n  seconds: 4\n", encoding="utf-8")
        with patch("pideck.sources.mock.run", seed_demo), local_server(config, mock=True, port=0) as url:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT}, reduced_motion="no-preference")
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                page.wait_for_selector(".tile")
                with httpx.Client(base_url=url, trust_env=False) as client:
                    for event in [
                        {"session_id": "mock-5", "name": "piDeck", "detail": "composer-2.5", "state": "working", "agent": "cursor"},
                        {"session_id": "mock-6", "name": "piDeck", "state": "idle", "agent": "cursor"},
                        {"session_id": "demo-explore", "name": "Explore", "parent_id": "mock-0", "state": "idle", "agent": "claude"},
                        {"session_id": "usage:cursor", "event": "UsageUpdate", "agent": "cursor", "usage": {
                            "primary": {"used_pct": 34, "window_minutes": 43200, "label": "Included"},
                            "secondary": {"used_pct": 6, "window_minutes": 43200, "label": "API"}}},
                    ]:
                        client.post("api/agents/event", json=event).raise_for_status()
                    page.locator('.usage-meter[data-provider="cursor"]', has_text="Included").wait_for()
                    capture(page, frames, 4)
                    page.evaluate("""() => {
                        const main = document.querySelector('.agents-main');
                        main.scrollTo({top: document.querySelector('[data-provider="claude"].agent-group').offsetTop - main.offsetTop,
                            behavior: 'smooth'});
                    }""")
                    capture(page, frames, 4)
                    page.evaluate("""() => {
                        const m = motions.get('mock-0'); m.thinkAt = 0; animateTiles();
                    }""")
                    capture(page, frames, 2)
                    client.post("api/agents/event", json={"session_id": "mock-0", "state": "done"}).raise_for_status()
                    page.locator("#alert.agent-done").wait_for()
                    capture(page, frames, 3)
                    page.locator("#alert").click()
                    tile = page.locator('.tile[data-id="mock-0"]')
                    tile.locator(".completion-badge").wait_for()
                    capture(page, frames, 1.5)
                    tile.click()
                    assert tile.locator(".completion-badge").count() == 0
                    capture(page, frames, 1)
                    page.evaluate("document.querySelector('.agents-main').scrollTo({top:10000,behavior:'smooth'})")
                    capture(page, frames, 2.5)
                    page.locator("#settings-btn").click()
                    capture(page, frames, 2)
                    page.locator("#settings-btn").click()
                    page.locator('#pager button[data-view="now_playing"]').click()
                    capture(page, frames, 3)
                    page.locator('#pager button[data-view="slack"]').click()
                    capture(page, frames, 3)
                    page.locator('#pager button[data-view="agents"]').click()
                    page.evaluate("document.querySelector('.agents-main').scrollTo({top:0,behavior:'smooth'})")
                    capture(page, frames, 2)
                browser.close()
    assert not errors, errors
    encode_gif(frames, destination)


"""# --- Command entry point ---"""


def main():
    """Parse an optional output path and record the README demo."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "docs/demo.gif")
    record(parser.parse_args().output)


if __name__ == "__main__":
    main()
