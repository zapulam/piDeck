"""Fake data so the whole UI can be developed and demoed without any accounts.

Run with `python -m pideck --mock` (or `mock: true` in config.yaml).
"""
from __future__ import annotations

import asyncio
import random
import time

from ..hub import Hub
from .agents import AgentEvent, AgentRegistry
from .slack import SlackFeed

TRACKS = [
    ("Midnight City", "M83", "Hurry Up, We're Dreaming", 243_000, "#ff6b6b", "#4d3a8e"),
    ("Redbone", "Childish Gambino", "Awaken, My Love!", 326_000, "#f4a259", "#5b3a29"),
    ("Genesis", "Grimes", "Visions", 255_000, "#7bdff2", "#b2f7ef"),
]
AGENT_NAMES = [
    "piDeck", "api-gateway", "etl-nightly", "churn-model", "mcp-server",
    "dash-v2", "chat-app", "forecast", "docs-site", "infra-tf", "slack-bot", "scratch",
]
SLACK = [
    ("dm", "Maya", "DM", "got a sec? the churn model numbers look off for Q3"),
    ("mention", "Devon", "#data-team", "@zack can you review the PR for the feature store?"),
    ("channel", "Priya", "#general", "Lunch at 12:30, tacos 🌮"),
    ("dm", "Sam", "DM", "deploy is green, nice work"),
]


def _art(c1: str, c2: str) -> str:
    svg = (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='300' height='300'>"
        f"<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
        f"<stop offset='0' stop-color='{c1}'/><stop offset='1' stop-color='{c2}'/></linearGradient></defs>"
        f"<rect width='300' height='300' fill='url(#g)'/>"
        f"<circle cx='150' cy='150' r='70' fill='none' stroke='white' stroke-opacity='.35' stroke-width='10'/>"
        f"</svg>"
    )
    from urllib.parse import quote

    return "data:image/svg+xml;utf8," + quote(svg)


async def seed(hub: Hub, agents: AgentRegistry, slack: SlackFeed) -> None:
    """A static, screenshot-friendly starting point."""
    now = time.time()
    states = ["working", "waiting", "working", "done", "done", "idle", "done", "working",
              "done", "done", "idle", "done"]
    for i, (name, st) in enumerate(zip(AGENT_NAMES, states)):
        await agents.handle(AgentEvent(session_id=f"mock-{i}", state=st, name=name,
                                       context_pct=random.Random(i).randint(8, 92)))
        a = agents.agents[f"mock-{i}"]
        a["state_since"] = a["updated_at"] = now - random.Random(i + 40).choice([20, 90, 400, 1500, 7200, 90000])
    await agents._publish()
    t = TRACKS[0]
    await hub.update("spotify", {
        "id": "mock-0", "title": t[0], "artist": t[1], "album": t[2], "art": _art(t[4], t[5]),
        "is_playing": True, "progress_ms": 71_000, "duration_ms": t[3],
    })
    for kind, user, chan, text in reversed(SLACK):
        await slack.push(kind=kind, user=user, channel=chan, text=text, ts=now - random.random() * 900)


async def run(hub: Hub, agents: AgentRegistry, slack: SlackFeed, lively: bool = True) -> None:
    await seed(hub, agents, slack)
    if not lively:
        return
    rng = random.Random()
    track = 0
    while True:
        await asyncio.sleep(rng.uniform(6, 12))
        roll = rng.random()
        if roll < 0.55:
            i = rng.randrange(len(AGENT_NAMES))
            sid = f"mock-{i}"
            cur = agents.agents.get(sid, {}).get("state", "idle")
            nxt = {"idle": "working", "working": rng.choice(["done", "waiting"]),
                   "waiting": "working", "done": "working"}[cur]
            await agents.handle(AgentEvent(session_id=sid, state=nxt, name=AGENT_NAMES[i],
                                           context_pct=rng.randint(8, 95)))
        elif roll < 0.8:
            kind, user, chan, text = rng.choice(SLACK)
            await slack.push(kind=kind, user=user, channel=chan, text=text)
        else:
            track = (track + 1) % len(TRACKS)
            t = TRACKS[track]
            await hub.update("spotify", {
                "id": f"mock-{track}", "title": t[0], "artist": t[1], "album": t[2],
                "art": _art(t[4], t[5]), "is_playing": True, "progress_ms": 0, "duration_ms": t[3],
            })
