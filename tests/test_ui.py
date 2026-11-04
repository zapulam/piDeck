"""Chromium checks for the real UI, websocket data, and responsive desktop layouts."""
from pathlib import Path

import httpx
import pytest

from pideck.desktop import local_server

pytestmark = pytest.mark.browser


"""# --- Browser fixtures ---"""


@pytest.fixture(autouse=True)
def static_demo(
        monkeypatch,
):
    """Seed demo sessions without random background events during browser assertions.

    Args:
        monkeypatch: Pytest helper that restores the demo runner after each test.
    """
    from pideck.sources import mock

    monkeypatch.setattr(mock, "run", lambda hub, agents, slack, lively=True: mock.seed(hub, agents, slack))


"""# --- Browser integration ---"""


@pytest.mark.parametrize("width,height", [(800, 480), (1280, 720), (520, 720), (360, 400)])
def test_agents_usage_and_cursor_snapshots_in_chromium(
        tmp_path,
        width,
        height,
):
    """Render websocket snapshots and context updates at Pi and desktop window sizes.

    Args:
        tmp_path: Isolated directory for server configuration.
        width: Browser viewport width in pixels.
        height: Browser viewport height in pixels.
    """
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
                assert outline == "rgb(87, 157, 255)"
                hop = page.locator('.tile[data-state="working"] canvas').first.evaluate("el => getComputedStyle(el).animationName")
                assert hop == "hop"
                assert page.locator("#pager button").first.evaluate("el => getComputedStyle(el).cursor") == "pointer"
                assert page.locator('.agent-group[data-provider="cursor"] h3').text_content() == "Cursor"
                with httpx.Client(base_url=url, trust_env=False) as client:
                    client.post("api/agents/event", json={"session_id": "ui-done", "state": "working", "name": "notes", "agent": "claude"})
                    client.post("api/agents/event", json={"session_id": "ui-done", "state": "done", "name": "notes", "agent": "claude"})
                    takeover = page.locator("#alert.agent-done")
                    takeover.wait_for()
                    assert takeover.locator("#alert-title").inner_text() == "notes finished"
                    assert takeover.locator("canvas").get_attribute("data-session-id") == "ui-done"
                    assert takeover.locator("canvas").get_attribute("data-accessory") in {
                        "cowboy", "graduation", "baseball", "sunglasses",
                    }
                    tile = page.locator('.tile[data-id="ui-done"]')
                    assert tile.locator(".completion-badge").count() == 1
                    assert page.locator(".done-card, #done-stack").count() == 0
                    # Dismissing the takeover leaves the session's checkmark intact.
                    takeover.click()
                    takeover.wait_for(state="hidden")
                    assert tile.locator(".completion-badge").count() == 1
                    page.reload()
                    tile.locator(".completion-badge").wait_for()
                    assert page.locator("#alert").is_hidden()
                    tile.click()
                    tile.locator(".completion-badge").wait_for(state="detached")
                    page.reload()
                    tile.wait_for()
                    assert tile.locator(".completion-badge").count() == 0
                    # Another completed turn in the same session gets a fresh checkmark.
                    client.post("api/agents/event", json={"session_id": "ui-done", "state": "working"})
                    client.post("api/agents/event", json={"session_id": "ui-done", "state": "done"})
                    takeover.wait_for()
                    page.keyboard.press("Escape")
                    tile.locator(".completion-badge").wait_for()
                    tile.focus()
                    page.keyboard.press("Enter")
                    tile.locator(".completion-badge").wait_for(state="detached")
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
                    page.wait_for_function('() => document.querySelector(\'[data-id="ui-cursor"] .context-label\')?.textContent === "Context unavailable"')
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


def test_random_sprite_motion_in_chromium(
        tmp_path,
):
    """Check independent motion clocks, rendered faces, and intermittent thought bubbles.

    Args:
        tmp_path: Isolated directory for server configuration.
    """
    from playwright.sync_api import sync_playwright

    with local_server(tmp_path / "config.yaml", mock=True, port=0) as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            try:
                page.goto(url)
                page.wait_for_selector('.tile[data-state="working"]')
                result = page.evaluate("""() => {
                    const first = creatureMotion('idle', 0), second = creatureMotion('idle', 0);
                    const blinkAt = first.blinkAt;
                    const blink = stepCreatureMotion(first, blinkAt);
                    const blinkEnded = stepCreatureMotion(first, blinkAt + 250);
                    const looking = stepCreatureMotion(second, second.lookAt);
                    const thinker = creatureMotion('working', 0);
                    const thought = stepCreatureMotion(thinker, thinker.thinkAt);
                    const pauseAt = thinker.thinkUntil + 1;
                    const pause = stepCreatureMotion(thinker, pauseAt);
                    const cv = document.createElement('canvas'); cv.width = cv.height = 12;
                    const pixels = (pose) => {
                        drawCreature(cv, {name: 'piDeck', state: 'idle', ...pose});
                        return cv.toDataURL();
                    };
                    const awake = pixels({});
                    const blinkPixels = pixels({blink: true});
                    const glancePixels = pixels({look: -1});
                    const working = pixels({state: 'working', frame: 0});
                    const workingLater = pixels({state: 'working', frame: 3});
                    const accessories = ACCESSORIES.map(accessory => pixels({state: 'celebrating', accessory}));
                    return {
                        independent: first.blinkAt !== second.blinkAt && first.lookAt !== second.lookAt,
                        blink: blink.blink, blinkEnded: blinkEnded.blink,
                        looking: Math.abs(looking.look) === 1,
                        thought: thought.thinking, pause: pause.thinking,
                        futureThought: thinker.thinkAt > pauseAt,
                        blinkPixels: awake !== blinkPixels, glancePixels: awake !== glancePixels,
                        noShine: working === workingLater,
                        accessories: new Set(accessories).size === 4 && accessories.every(p => p !== awake),
                    };
                }""")
                assert result == {
                    "independent": True, "blink": True, "blinkEnded": False, "looking": True,
                    "thought": True, "pause": False, "futureThought": True,
                    "blinkPixels": True, "glancePixels": True, "noShine": True, "accessories": True,
                }
                page.evaluate("""() => {
                    const tile = document.querySelector('.tile[data-state="working"]');
                    const m = motions.get(tile.dataset.id); m.thinkAt = 0;
                    window.testMotion = m; window.testMotionId = tile.dataset.id;
                }""")
                page.wait_for_selector('.tile[data-state="working"] .thinking:not([hidden])')
                assert page.locator('.thinking:not([hidden]) i').count() >= 3
                assert page.evaluate("""() => {
                    renderAll(); return motions.get(window.testMotionId) === window.testMotion;
                }""")
                page.evaluate("""() => {
                    for (const m of motions.values()) { m.thinkUntil = 0; m.thinkAt = Infinity; }
                }""")
                page.wait_for_function('() => document.querySelectorAll(".thinking:not([hidden])").length === 0')
            finally:
                browser.close()


def test_completion_queue_and_same_name_sessions_in_chromium(
        tmp_path,
):
    """Keep completion sprites and markers distinct and expire queued takeovers.

    Args:
        tmp_path: Isolated directory for server configuration.
    """
    from playwright.sync_api import sync_playwright

    config = tmp_path / "config.yaml"
    config.write_text("alerts:\n  seconds: 0.6\n", encoding="utf-8")
    with local_server(config, mock=True, port=0) as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            try:
                page.goto(url)
                page.wait_for_selector(".tile")
                with httpx.Client(base_url=url, trust_env=False) as client:
                    for sid in ("same:111", "same:222"):
                        client.post("api/agents/event", json={"session_id": sid, "state": "working", "name": "same"})
                    page.locator('.tile[data-id="same:222"]').wait_for()
                    for sid in ("same:111", "same:222"):
                        client.post("api/agents/event", json={"session_id": sid, "state": "done"})
                    page.wait_for_function('() => document.querySelector("#alert-sprite").dataset.sessionId === "same:111"')
                    assert page.evaluate("""() => {
                        const cv = document.createElement('canvas'); cv.width = cv.height = 12;
                        drawCreature(cv, {state: 'celebrating', seed: 'same:111',
                            accessory: document.querySelector('#alert-sprite').dataset.accessory});
                        return cv.toDataURL() === document.querySelector('#alert-sprite').toDataURL();
                    }""")
                    page.wait_for_function('() => document.querySelector("#alert-sprite").dataset.sessionId === "same:222"')
                    page.locator("#alert").wait_for(state="hidden")
                    assert page.locator('.tile[data-name="same"] .completion-badge').count() == 2
                    # Keyboard acknowledgment affects only the selected session.
                    first = page.locator('.tile[data-id="same:111"]')
                    first.focus()
                    page.keyboard.press("Space")
                    first.locator(".completion-badge").wait_for(state="detached")
                    assert page.locator('.tile[data-id="same:222"] .completion-badge').count() == 1
                    client.post("api/agents/event", json={"session_id": "same:222", "state": "working"})
                    page.locator('.tile[data-id="same:222"] .completion-badge').wait_for(state="detached")
            finally:
                browser.close()
