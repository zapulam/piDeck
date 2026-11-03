"""Chromium checks for the real UI, websocket data, and responsive desktop layouts."""
from pathlib import Path

import httpx
import pytest

from pideck.desktop import local_server

pytestmark = pytest.mark.browser


"""# --- Browser integration ---"""


@pytest.mark.parametrize("width,height", [(800, 480), (1280, 720), (520, 720), (360, 400)])
def test_agents_usage_and_cursor_snapshots_in_chromium(
        tmp_path,
        width,
        height,
):
    """Render websocket snapshots and context updates at Pi and desktop window sizes."""
    from playwright.sync_api import sync_playwright

    with local_server(tmp_path / "config.yaml", mock=True, port=0) as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": width, "height": height})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                page.goto(url)
                page.wait_for_selector('.usage-card[data-provider="cursor"]')
                assert page.locator(".usage-card").count() == 3
                assert page.locator('.usage-card[data-provider="cursor"] [role="progressbar"]').count() == 0
                assert page.locator("#v-agents").evaluate("el => el.scrollWidth <= el.clientWidth")
                with httpx.Client(base_url=url, trust_env=False) as client:
                    client.post("api/agents/event", json={"session_id": "ui-cursor", "state": "working", "agent": "cursor"})
                    client.post("api/agents/event", json={"session_id": "ui-cursor", "event": "UsageUpdate", "agent": "cursor", "context_pct": 85})
                    tile = page.locator('[data-id="ui-cursor"]')
                    tile.locator('.context-track[aria-valuenow="85"]').wait_for()
                    assert "Before compact 85%" in tile.inner_text()
                    client.post("api/agents/event", json={"session_id": "ui-cursor", "event": "PostToolUse", "agent": "cursor", "context_pct": None})
                    page.wait_for_function('document.querySelector(\'[data-id="ui-cursor"] .context-label\')?.textContent === "Context unavailable"')
                assert not errors
            finally:
                screenshots = Path("test-results/browser")
                screenshots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshots / f"agents-{width}x{height}.png"))
                browser.close()
