"""Slack notifications over Socket Mode (outbound websocket, so no public URL needed).

Needs a Slack app with Socket Mode on, an app-level token (xapp-, scope connections:write),
a bot token (xoxb-), and event subscriptions: message.im, app_mention (+ message.channels for
extra channels). The bot only sees conversations it is part of.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Optional

from ..hub import Hub

log = logging.getLogger("pideck.slack")

_USER = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
_CHAN = re.compile(r"<#[A-Z0-9]+\|([^>]+)>")
_LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]+))?>")
_SPECIAL = re.compile(r"<!(?:here|channel|everyone)(?:\|[^>]*)?>")


def clean_text(text: str, names: dict[str, str] | None = None) -> str:
    """Make Slack mrkdwn readable on a tiny screen."""
    names = names or {}
    text = _USER.sub(lambda m: "@" + names.get(m.group(1), "someone"), text)
    text = _CHAN.sub(lambda m: "#" + m.group(1), text)
    text = _LINK.sub(lambda m: m.group(2) or re.sub(r"^https?://", "", m.group(1)), text)
    text = _SPECIAL.sub("@channel", text)
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()


class SlackFeed:
    """Holds the recent-message list and decides what raises an alert."""

    def __init__(self, hub: Hub, cfg: dict, alert_seconds: float = 8) -> None:
        self.hub = hub
        self.keep = int(cfg.get("keep", 30))
        self.alert_on = set(cfg.get("alert_on", ["dm", "mention"]))
        self.alert_seconds = alert_seconds
        self.messages: list[dict] = []

    async def push(self, *, kind: str, user: str, channel: str, text: str, ts: float | None = None) -> None:
        """kind: dm | mention | channel"""
        msg = {
            "id": f"{int((ts or time.time()) * 1000)}-{len(self.messages)}",
            "kind": kind,
            "user": user,
            "channel": channel,
            "text": text,
            "ts": ts or time.time(),
        }
        self.messages = ([msg] + self.messages)[: self.keep]
        await self.hub.update("slack", self.messages)
        if kind in self.alert_on:
            where = "DM" if kind == "dm" else channel
            await self.hub.alert(
                "slack", f"{user} · {where}", text[:200], seconds=self.alert_seconds
            )

    async def clear(self) -> None:
        self.messages = []
        await self.hub.update("slack", [])


class SlackSource:
    def __init__(self, feed: SlackFeed, cfg: dict) -> None:
        self.feed = feed
        self.watch = set(cfg.get("channels", []))
        self.bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
        self.app_token = os.environ.get("SLACK_APP_TOKEN", "")
        self._names: dict[str, str] = {}
        self._chans: dict[str, str] = {}
        self._bot_id: Optional[str] = None
        self._web: Any = None

    @property
    def configured(self) -> bool:
        return self.bot_token.startswith("xoxb-") and self.app_token.startswith("xapp-")

    async def _user(self, uid: str) -> str:
        if uid not in self._names:
            try:
                r = await self._web.users_info(user=uid)
                p = r["user"]
                self._names[uid] = (
                    p.get("profile", {}).get("display_name") or p.get("real_name") or p["name"]
                )
            except Exception:
                self._names[uid] = "someone"
        return self._names[uid]

    async def _channel(self, cid: str) -> str:
        if cid not in self._chans:
            try:
                r = await self._web.conversations_info(channel=cid)
                self._chans[cid] = "#" + r["channel"].get("name", cid)
            except Exception:
                self._chans[cid] = cid
        return self._chans[cid]

    async def handle_event(self, event: dict) -> None:
        """Pure-ish event handler (separately testable from the socket plumbing)."""
        if event.get("bot_id") or event.get("subtype") or event.get("user") == self._bot_id:
            return
        uid = event.get("user")
        if not uid:
            return
        etype = event.get("type")
        if etype == "app_mention":
            kind = "mention"
        elif etype == "message" and event.get("channel_type") == "im":
            kind = "dm"
        elif etype == "message" and event.get("channel") in self.watch:
            kind = "channel"
        else:
            return
        # resolve names for any <@U..> mentions inside the text too
        for m in _USER.finditer(event.get("text", "")):
            await self._user(m.group(1))
        await self.feed.push(
            kind=kind,
            user=await self._user(uid),
            channel="DM" if kind == "dm" else await self._channel(event.get("channel", "")),
            text=clean_text(event.get("text", ""), self._names),
            ts=float(event.get("ts") or time.time()),
        )

    async def run(self) -> None:
        if not self.configured:
            log.warning("Slack enabled but SLACK_BOT_TOKEN/SLACK_APP_TOKEN missing; skipping")
            return
        from slack_sdk.socket_mode.aiohttp import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse
        from slack_sdk.web.async_client import AsyncWebClient

        self._web = AsyncWebClient(token=self.bot_token)
        try:
            self._bot_id = (await self._web.auth_test())["user_id"]
        except Exception as e:
            log.warning("slack auth_test failed: %s", e)
        client = SocketModeClient(app_token=self.app_token, web_client=self._web)

        async def on_request(c: SocketModeClient, req: Any) -> None:
            await c.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            if req.type == "events_api":
                try:
                    await self.handle_event(req.payload.get("event", {}))
                except Exception:
                    log.exception("slack event failed")

        client.socket_mode_request_listeners.append(on_request)
        await client.connect()  # the SDK reconnects on its own
        import asyncio

        await asyncio.Event().wait()
