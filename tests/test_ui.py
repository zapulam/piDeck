"""

Chromium checks for the real UI, websocket data, and responsive desktop layouts.

"""

from __future__ import annotations

from typing import Any, Callable
from playwright.sync_api import Page

from pathlib import Path

import httpx
import pytest

from pideck.desktop import local_server

pytestmark = pytest.mark.browser


# --- Browser fixtures ---


@pytest.fixture(autouse=True)
def static_demo(
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    """
    Seed demo sessions without random background events during browser assertions.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest helper that restores the demo runner after each
            test.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    from pideck.sources import mock

    monkeypatch.setattr(mock, "run", lambda hub, agents, slack, lively=True: mock.seed(hub, agents, slack))


# --- Pointer interaction helpers ---


def drag_usage_panel(
    page: Page,
    edge: str,
    rotate: int = 0,
    on_drag: Callable[[], Any] | None = None,
    ) -> None:
    """
    Drag the usage grip to a visible screen edge and check its highlighted target.

    Args:
        page (Page): Browser page showing the Agents view.
        edge (str): Desired logical docking edge.
        rotate (int): Software screen rotation used by this test, either zero or 90.
        on_drag (Callable[[], Any] | None): Optional callback invoked while the pointer is captured.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    grip = page.locator(".usage-grab")
    grip.scroll_into_view_if_needed()
    source = grip.bounding_box()
    panel = page.locator("#v-agents").bounding_box()
    screen_edge = {"top": "right", "bottom": "left", "left": "top", "right": "bottom"}[edge] if rotate == 90 else edge
    x, y = panel["x"] + panel["width"] / 2, panel["y"] + panel["height"] / 2
    if screen_edge == "left":
        x = panel["x"] + 10
    elif screen_edge == "right":
        x = panel["x"] + panel["width"] - 10
    elif screen_edge == "top":
        y = panel["y"] + 10
    else:
        y = panel["y"] + panel["height"] - 10
    page.mouse.move(source["x"] + source["width"] / 2, source["y"] + source["height"] / 2)
    page.mouse.down()
    page.mouse.move(x, y, steps=12)
    page.locator(f'.dock-target.chosen[data-drop-dock="{edge}"]').wait_for()
    if on_drag:
        on_drag()
    page.mouse.up()


# --- Browser integration ---


@pytest.mark.parametrize("width,height", [(800, 480), (1280, 720), (520, 720), (360, 400)])
def test_agents_usage_and_cursor_snapshots_in_chromium(
    tmp_path: Path,
    width: int,
    height: int,
    ) -> None:
    """
    Render websocket snapshots and context updates at Pi and desktop window sizes.

    Args:
        tmp_path (Path): Isolated directory for server configuration.
        width (int): Browser viewport width in pixels.
        height (int): Browser viewport height in pixels.

    Returns:
        None: Assertions or fixture setup completed successfully.
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
                    page.wait_for_function('() => currentView() === "settings"')
                    assert page.locator("#settings-panel").count() == 0
                    page.locator('#v-settings [data-page="slack"][aria-pressed="true"]').click()
                    page.locator('#pager button[data-view="slack"]').wait_for(state="detached")
                    page.reload()
                    page.wait_for_selector("#settings-btn")
                    assert page.locator('#pager button[data-view="slack"]').count() == 0
                    page.locator("#settings-btn").click()
                    page.locator('#v-settings [data-page="slack"]').click()
                    page.locator('#pager button[data-view="slack"]').wait_for()
                    page.locator('#pager button[data-view="agents"]').click()
                assert page.locator("#v-agents").evaluate("el => el.scrollWidth <= el.clientWidth")
                with httpx.Client(base_url=url, trust_env=False) as client:
                    client.post("api/agents/event", json={"session_id": "ui-cursor", "state": "working", "agent": "cursor"})
                    client.post("api/agents/event", json={"session_id": "ui-cursor", "event": "UsageUpdate", "agent": "cursor", "context_pct": 85})
                    tile = page.locator('[data-id="ui-cursor"]')
                    tile.locator('.context-track[aria-valuenow="85"]').wait_for()
                    assert "Before compact" in tile.inner_text()
                    assert "85%" not in tile.inner_text()
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
                    page.locator('.usage-meter[data-provider="cursor"] [data-window="primary"][aria-valuenow="40"]').wait_for()
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
    tmp_path: Path,
    ) -> None:
    """
    Check independent motion clocks, rendered faces, and intermittent thought bubbles.

    Args:
        tmp_path (Path): Isolated directory for server configuration.

    Returns:
        None: Assertions or fixture setup completed successfully.
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
    tmp_path: Path,
    ) -> None:
    """
    Keep completion sprites and markers distinct and expire queued takeovers.

    Args:
        tmp_path (Path): Isolated directory for server configuration.

    Returns:
        None: Assertions or fixture setup completed successfully.
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


# --- Custom agent layouts and compact usage ---


@pytest.mark.parametrize("width,height,rotate", [(800, 480, 0), (360, 400, 0), (800, 480, 90)])
def test_usage_panel_dragging_and_persistence(
    tmp_path: Path,
    width: int,
    height: int,
    rotate: int,
    ) -> None:
    """
    Dock on each edge with real pointer gestures, including a rotated display.

    Args:
        tmp_path (Path): Isolated server configuration directory.
        width (int): Browser viewport width.
        height (int): Browser viewport height.
        rotate (int): Software stage rotation in degrees.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    from playwright.sync_api import expect, sync_playwright

    with local_server(tmp_path / "config.yaml", mock=True, port=0) as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": width, "height": height})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                page.goto(url + f"?rotate={rotate}")
                page.wait_for_selector(".usage-grab")
                with httpx.Client(base_url=url, trust_env=False) as client:
                    for edge in ("top", "left", "right", "bottom"):
                        drag_usage_panel(page, edge, rotate, on_drag=lambda: client.post("api/agents/event", json={
                            "session_id": "during-drag", "state": "working", "agent": "codex",
                        }).raise_for_status())
                        expect(page.locator("#v-agents")).to_have_attribute("data-dock", edge)
                        assert page.locator('[data-id="during-drag"]').count() == 1
                        geometry = page.locator("#v-agents").evaluate("""el => {
                            const u = el.querySelector('.usage-dock'), m = el.querySelector('.agents-main');
                            return {ux:u.offsetLeft, uy:u.offsetTop, uw:u.offsetWidth, uh:u.offsetHeight,
                                mx:m.offsetLeft, my:m.offsetTop, mw:m.offsetWidth, mh:m.offsetHeight,
                                fits:el.scrollWidth <= el.clientWidth && m.scrollWidth <= m.clientWidth};
                        }""")
                        assert geometry["fits"]
                        if edge == "top":
                            assert geometry["uy"] + geometry["uh"] <= geometry["my"]
                        elif edge == "bottom":
                            assert geometry["my"] + geometry["mh"] <= geometry["uy"]
                        elif edge == "left":
                            assert geometry["ux"] + geometry["uw"] <= geometry["mx"]
                        else:
                            assert geometry["mx"] + geometry["mw"] <= geometry["ux"]
                        page.reload()
                        expect(page.locator("#v-agents")).to_have_attribute("data-dock", edge)
                assert not errors
            finally:
                screenshots = Path("test-results/browser")
                screenshots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshots / f"usage-dock-{width}x{height}-rot{rotate}.png"))
                browser.close()


def test_provider_group_dragging_and_keyboard_order(
    tmp_path: Path,
    ) -> None:
    """
    Move an offscreen group to the top, persist it, and support keyboard/cancel.

    Args:
        tmp_path (Path): Isolated server configuration directory.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    from playwright.sync_api import sync_playwright

    with local_server(tmp_path / "config.yaml", mock=True, port=0) as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 800, "height": 480})
            try:
                page.goto(url)
                codex = page.locator('[data-drag-group="codex"]')
                codex.wait_for()
                codex.scroll_into_view_if_needed()
                source, main = codex.bounding_box(), page.locator(".agents-main").bounding_box()
                page.mouse.move(source["x"] + 20, source["y"] + source["height"] / 2)
                page.mouse.down()
                page.mouse.move(main["x"] + 100, main["y"] + 8, steps=15)
                page.wait_for_function('() => document.querySelector(".agents-main").scrollTop <= 1')
                page.mouse.up()
                assert page.locator(".agent-group").evaluate_all("els => els.map(el => el.dataset.provider)") == ["codex", "cursor", "claude"]
                codex.focus()
                page.keyboard.press("ArrowDown")
                assert page.locator(".agent-group").first.get_attribute("data-provider") == "cursor"
                page.keyboard.press("ArrowUp")
                assert page.locator(".agent-group").first.get_attribute("data-provider") == "codex"
                page.reload()
                codex.wait_for()
                assert page.locator(".agent-group").first.get_attribute("data-provider") == "codex"
                source = codex.bounding_box()
                page.mouse.move(source["x"] + 20, source["y"] + 10)
                page.mouse.down()
                page.mouse.move(source["x"] + 30, source["y"] + 90, steps=10)
                page.locator(".drag-preview").wait_for()
                page.keyboard.press("Escape")
                page.mouse.up()
                assert page.locator(".drag-preview").count() == 0
                assert page.locator(".agent-group").first.get_attribute("data-provider") == "codex"
            finally:
                browser.close()


def test_usage_panel_touch_drag_and_keyboard_docking(
    tmp_path: Path,
    ) -> None:
    """
    Use a real touch gesture to dock usage without swiping to another page.

    Args:
        tmp_path (Path): Isolated server configuration directory.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    from playwright.sync_api import expect, sync_playwright

    with local_server(tmp_path / "config.yaml", mock=True, port=0) as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 360, "height": 720}, has_touch=True)
            page = context.new_page()
            try:
                page.goto(url)
                grip = page.locator(".usage-grab")
                grip.wait_for()
                source, panel = grip.bounding_box(), page.locator("#v-agents").bounding_box()
                start = {"x": source["x"] + source["width"] / 2, "y": source["y"] + source["height"] / 2}
                end = {"x": panel["x"] + 10, "y": panel["y"] + panel["height"] / 2}
                session = context.new_cdp_session(page)
                session.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [start]})
                for step in range(1, 13):
                    point = {axis: start[axis] + (end[axis] - start[axis]) * step / 12 for axis in ("x", "y")}
                    session.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [point]})
                session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
                expect(page.locator("#v-agents")).to_have_attribute("data-dock", "left")
                assert page.evaluate("currentView()") == "agents"
                page.locator(".usage-grab").focus()
                page.keyboard.press("ArrowRight")
                expect(page.locator("#v-agents")).to_have_attribute("data-dock", "right")
                page.keyboard.press("ArrowUp")
                expect(page.locator("#v-agents")).to_have_attribute("data-dock", "top")
                assert page.evaluate("currentView()") == "agents"
            finally:
                browser.close()


def test_usage_bar_colors_and_unknown_values(
    tmp_path: Path,
    ) -> None:
    """
    Interpolate solid bar colors toward red and distinguish unavailable values from zero.

    Args:
        tmp_path (Path): Isolated server configuration directory.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    from playwright.sync_api import expect, sync_playwright

    with local_server(tmp_path / "config.yaml", mock=True, port=0) as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            try:
                page.goto(url)
                bar = page.locator('.usage-meter[data-provider="codex"] .usage-bar[data-window="primary"]')
                bar.wait_for()
                colors = []
                with httpx.Client(base_url=url, trust_env=False) as client:
                    for pct in (0, 35, 70, 85, 100):
                        client.post("api/agents/event", json={
                            "session_id": "usage:codex", "event": "UsageUpdate", "agent": "codex",
                            "usage": {"primary": {"used_pct": pct, "window_minutes": 300}},
                        }).raise_for_status()
                        expect(bar).to_have_attribute("aria-valuenow", str(pct))
                        expect(bar).to_have_attribute("data-available", "true")
                        expect(bar).to_have_text("")
                        fill = bar.locator("div")
                        assert fill.evaluate("el => getComputedStyle(el).backgroundImage") == "none"
                        colors.append(fill.evaluate("el => getComputedStyle(el).backgroundColor"))
                        assert bar.evaluate("el => el.offsetWidth") == 24
                    assert colors[0] == "rgb(95, 211, 176)"
                    assert colors[2] == "rgb(255, 210, 103)"
                    assert colors[4] == "rgb(255, 64, 64)"
                    assert len(set(colors)) == 5
                    client.post("api/agents/event", json={
                        "session_id": "usage:codex", "event": "UsageUpdate", "agent": "codex",
                        "usage": {"primary": {"used_pct": 40, "window_minutes": 300, "resets_at": 1}},
                    }).raise_for_status()
                    expect(bar).to_have_attribute("data-available", "false")
                    expect(bar).to_have_text("")
                    assert bar.get_attribute("aria-valuenow") is None
            finally:
                browser.close()


@pytest.mark.parametrize("width,height,rotate", [(800, 480, 0), (360, 400, 0), (800, 480, 90)])
def test_compact_usage_focus_and_live_session_updates(
    tmp_path: Path,
    width: int,
    height: int,
    rotate: int,
    ) -> None:
    """
    Toggle the heading, preserve live usage focus, and revisit sessions through collapsed sprites.

    Args:
        tmp_path (Path): Isolated directory for server configuration and saved settings.
        width (int): Physical browser viewport width in pixels.
        height (int): Physical browser viewport height in pixels.
        rotate (int): Software display rotation used to exercise logical layout coordinates.

    Returns:
        None: Assertions complete when compact bars, counts, logos, and session navigation work.
    """
    from playwright.sync_api import expect, sync_playwright

    with local_server(tmp_path / "config.yaml", mock=True, port=0) as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": width, "height": height})
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append(request.url))
            try:
                page.goto(url + f"?rotate={rotate}")
                page.locator(".tile").first.wait_for()
                section = page.locator("#v-agents")
                expect(section).to_have_attribute("data-dock", "right")
                count = page.locator(".tile").count()
                expect(page.locator(".session-count")).to_have_text(f"{count} sessions")
                assert page.locator(".usage-dock").inner_text().strip() == ""
                assert page.locator(".usage-dock").evaluate("el => el.offsetWidth") == 44
                assert page.locator(".usage-bar").first.evaluate("el => el.offsetWidth") == 24
                assert page.locator(".usage-donut").count() == 0
                assert page.locator("#settings-btn").get_attribute("aria-label") == "Settings"
                assert page.locator("#settings-btn").evaluate("el => el.offsetLeft") <= 10
                assert page.locator("#settings-btn").inner_text() == ""
                assert page.locator("#pager-pages #settings-btn").count() == 0
                page.wait_for_function("""() => [...document.querySelectorAll('.provider-logo')]
                    .every(img => img.complete && img.naturalWidth > 0)""")
                nav_font = page.locator('#pager [data-view="agents"]').evaluate("el => getComputedStyle(el).fontFamily")
                assert page.locator(".tile .name").first.evaluate("el => getComputedStyle(el).fontFamily") == nav_font
                toggle = page.locator("h2 [data-agent-view-toggle]")
                expect(toggle).to_have_text("Agents")
                toggle.press("Enter")
                expect(section).to_have_attribute("data-mode", "usage")
                expect(toggle).to_have_text("Usage")
                assert page.locator(".mini-session").count() == count
                assert page.locator(".tile").count() == 0
                assert page.evaluate("currentView()") == "agents"
                assert page.locator(".usage-provider").count() == 3
                assert page.locator(".usage-window .usage-bar").first.evaluate("el => el.offsetWidth") <= 180
                with httpx.Client(base_url=url, trust_env=False) as client:
                    client.post("api/agents/event", json={
                        "session_id": "focus-test", "state": "working", "agent": "codex", "name": "focus-test",
                    }).raise_for_status()
                    expect(page.locator(".session-count")).to_have_text(f"{count + 1} sessions")
                    expect(section).to_have_attribute("data-mode", "usage")
                    assert page.locator(".mini-session").count() == count + 1
                    client.post("api/agents/event", json={
                        "session_id": "usage:cursor", "event": "UsageUpdate", "agent": "cursor",
                        "usage": {"primary": {"used_pct": 38, "window_minutes": 43200, "label": "Included"},
                                  "secondary": {"used_pct": 94, "window_minutes": 43200, "label": "API"}},
                    }).raise_for_status()
                    cursor = page.locator('.usage-provider[data-provider="cursor"]')
                    expect(cursor.locator('[data-window="primary"][role="progressbar"]')).to_have_attribute("aria-valuenow", "38")
                    assert cursor.locator(".usage-window").evaluate_all("els => els.map(el => el.dataset.window)") == ["secondary", "primary"]
                    expect(cursor.locator(".usage-window-heading").first).to_contain_text("API")
                    expect(cursor.locator(".usage-window-heading").last).to_contain_text("Included")
                    client.post("api/agents/event", json={"session_id": "focus-test", "state": "done"}).raise_for_status()
                    page.locator("#alert.agent-done").wait_for()
                    page.locator("#alert").click()
                    mini = page.locator('.mini-session[data-id="focus-test"]')
                    mini.locator(".mini-completion").wait_for()
                    mini.scroll_into_view_if_needed()
                    mini.click()
                    expect(section).to_have_attribute("data-mode", "agents")
                    expect(page.locator(".session-count")).to_have_text(f"{count + 1} sessions")
                    assert page.locator('.tile[data-id="focus-test"] .completion-badge').count() == 0
                    page.locator('.usage-meter[data-provider="cursor"]').click()
                    expect(section).to_have_attribute("data-mode", "usage")
                    expect(cursor).to_have_attribute("data-selected", "true")
                assert not errors
                assert all(request.startswith(url) or request.startswith("data:") for request in requests)
            finally:
                screenshots = Path("test-results/browser")
                screenshots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshots / f"usage-focus-{width}x{height}-rot{rotate}.png"))
                browser.close()


def test_settings_page_is_always_available(
    tmp_path: Path,
    ) -> None:
    """
    Keep the full Settings page accessible when every other page is switched off.

    Args:
        tmp_path (Path): Isolated server configuration directory.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    from playwright.sync_api import expect, sync_playwright

    with local_server(tmp_path / "config.yaml", mock=True, port=0) as url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 360, "height": 400})
            try:
                page.goto(url)
                page.locator("#settings-btn").click()
                page.wait_for_function('() => currentView() === "settings"')
                assert page.locator("#track > section").first.get_attribute("id") == "v-settings"
                page.wait_for_function("() => Math.abs(document.querySelector('#track').scrollLeft) < 2")
                assert page.locator("#settings-panel").count() == 0
                for name in ("agents", "now_playing", "slack"):
                    page.locator(f'#v-settings [data-page="{name}"]').click()
                    expect(page.locator(f'#v-settings [data-page="{name}"]')).to_have_attribute("aria-pressed", "false")
                assert page.locator("#pager button").count() == 1
                assert page.locator("#pager button").get_attribute("aria-label") == "Settings"
                assert page.locator("#pager button svg").count() == 1
                page.reload()
                page.wait_for_selector("#v-settings")
                assert page.locator("#pager button").count() == 1
                page.locator('#v-settings [data-page="agents"]').click()
                page.locator('#pager button[data-view="agents"]').click()
                page.wait_for_selector(".usage-grab")
            finally:
                browser.close()
