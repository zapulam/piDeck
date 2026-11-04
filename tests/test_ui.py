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
                page.wait_for_selector('.usage-meter[data-provider="cursor"]')
                assert page.locator(".usage-meter").count() == 3
                assert page.locator('.usage-meter[data-provider="cursor"] [role="progressbar"]').count() == 0
                outline = page.locator('.tile[data-state="working"]').first.evaluate("el => getComputedStyle(el).borderTopColor")
                assert outline != "rgb(38, 46, 64)"
                hop = page.locator('.tile[data-state="working"] canvas').first.evaluate("el => getComputedStyle(el).animationName")
                assert hop == "hop"
                assert page.locator("#pager button").first.evaluate("el => getComputedStyle(el).cursor") == "pointer"
                assert page.locator('.agent-group[data-provider="cursor"] h3').text_content() == "Cursor"
                with httpx.Client(base_url=url, trust_env=False) as client:
                    client.post("api/agents/event", json={"session_id": "ui-done", "state": "working", "name": "notes", "agent": "claude"})
                    client.post("api/agents/event", json={"session_id": "ui-done", "state": "done", "name": "notes", "agent": "claude"})
                    card = page.locator(".done-card", has_text="notes finished")
                    card.wait_for()
                    title = card.locator("b").evaluate("el => getComputedStyle(el).color")
                    assert title == "rgb(255, 255, 255)"
                    card.click()
                    page.locator(".done-card").wait_for(state="hidden")
                    page.locator("#settings-btn").click()
                    page.locator('#settings-panel [data-page="slack"][aria-pressed="true"]').click()
                    page.locator('#pager button[data-view="slack"]').wait_for(state="detached")
                    page.reload()
                    page.wait_for_selector("#settings-btn")
                    assert page.locator('#pager button[data-view="slack"]').count() == 0
                    page.locator("#settings-btn").click()
                    page.locator('#settings-panel [data-page="slack"]').click()
                    page.locator('#pager button[data-view="slack"]').wait_for()
                assert page.locator("#v-agents").evaluate("el => el.scrollWidth <= el.clientWidth")
                with httpx.Client(base_url=url, trust_env=False) as client:
                    client.post("api/agents/event", json={"session_id": "ui-cursor", "state": "working", "agent": "cursor"})
                    client.post("api/agents/event", json={"session_id": "ui-cursor", "event": "UsageUpdate", "agent": "cursor", "context_pct": 85})
                    tile = page.locator('[data-id="ui-cursor"]')
                    tile.locator('.context-track[aria-valuenow="85"]').wait_for()
                    assert "Before compact 85%" in tile.inner_text()
                    client.post("api/agents/event", json={"session_id": "ui-cursor", "event": "PostToolUse", "agent": "cursor", "context_pct": None})
                    page.wait_for_function('document.querySelector(\'[data-id="ui-cursor"] .context-label\')?.textContent === "Context unavailable"')
                    client.post("api/agents/event", json={"session_id": "cursor:aaa111", "state": "done", "name": "piDeck", "agent": "cursor"})
                    client.post("api/agents/event", json={"session_id": "cursor:bbb222", "state": "done", "name": "piDeck", "agent": "cursor", "detail": "composer-2.5"})
                    page.locator('[data-id="cursor:bbb222"] .tag', has_text="composer-2.5").wait_for()
                    same = page.locator('.agent-group[data-provider="cursor"] .tile[data-name="piDeck"]')
                    tags = same.locator(".tag").all_inner_texts()
                    assert len(tags) == 2 and len(set(tags)) == 2
                    seeds = same.evaluate_all("els => els.map(el => el.dataset.seed)")
                    assert len(set(seeds)) == 2
                    client.post("api/agents/event", json={
                        "session_id": "usage:cursor", "event": "UsageUpdate", "agent": "cursor",
                        "usage": {"primary": {"used_pct": 40, "window_minutes": 43200, "label": "Included"},
                                  "secondary": {"used_pct": 5, "window_minutes": 43200, "label": "API"}},
                    })
                    page.locator('.usage-meter[data-provider="cursor"]', has_text="Included").wait_for()
                    client.post("api/agents/event", json={
                        "session_id": "ui-done:explore", "state": "working", "name": "Explore",
                        "agent": "claude", "parent_id": "ui-done",
                    })
                    page.locator('[data-id="ui-done:explore"] .tag', has_text="sub of notes").wait_for()
                assert not errors
            finally:
                screenshots = Path("test-results/browser")
                screenshots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshots / f"agents-{width}x{height}.png"))
                browser.close()
