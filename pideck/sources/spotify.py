"""Spotify now-playing via the Web API (authorization-code flow + refresh token).

Reading playback works on any account; the transport controls need Premium.
Get a refresh token once with `python -m pideck.spotify_auth`.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import Any, Optional

import httpx

from ..hub import Hub

log = logging.getLogger("pideck.spotify")
API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = "user-read-currently-playing user-read-playback-state user-modify-playback-state"


class Spotify:
    def __init__(self, hub: Hub, cfg: dict) -> None:
        self.hub = hub
        self.poll = float(cfg.get("poll_seconds", 3))
        self.client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
        self.client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
        self.refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN", "")
        self._access: Optional[str] = None
        self._expires = 0.0
        self._last: Optional[dict] = None
        self._http = httpx.AsyncClient(timeout=10)

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    async def _token(self) -> str:
        if self._access and time.time() < self._expires - 30:
            return self._access
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        r = await self._http.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
            headers={"Authorization": f"Basic {basic}"},
        )
        r.raise_for_status()
        j = r.json()
        self._access = j["access_token"]
        self._expires = time.time() + int(j.get("expires_in", 3600))
        self.refresh_token = j.get("refresh_token", self.refresh_token)
        return self._access

    async def _request(self, method: str, path: str) -> httpx.Response:
        r = await self._http.request(
            method, f"{API}{path}", headers={"Authorization": f"Bearer {await self._token()}"}
        )
        if r.status_code == 401:  # token revoked/expired early: force a refresh and retry once
            self._access = None
            r = await self._http.request(
                method, f"{API}{path}", headers={"Authorization": f"Bearer {await self._token()}"}
            )
        return r

    @staticmethod
    def parse(j: dict[str, Any]) -> Optional[dict]:
        """Turn a currently-playing payload into the compact shape the UI uses."""
        item = j.get("item")
        if not item or j.get("currently_playing_type") not in (None, "track", "episode"):
            return None
        images = (item.get("album") or item.get("show") or {}).get("images") or []
        # prefer the ~300px image; fall back to whatever exists
        art = (images[1] if len(images) > 1 else images[0])["url"] if images else None
        artists = item.get("artists") or []
        artist = ", ".join(a["name"] for a in artists) or (item.get("show") or {}).get("name", "")
        return {
            "id": item.get("id"),
            "title": item.get("name", ""),
            "artist": artist,
            "album": (item.get("album") or {}).get("name", ""),
            "art": art,
            "is_playing": bool(j.get("is_playing")),
            "progress_ms": int(j.get("progress_ms") or 0),
            "duration_ms": int(item.get("duration_ms") or 0),
        }

    def _changed(self, new: Optional[dict]) -> bool:
        old = self._last
        if new is None or old is None:
            return new != old
        if (old["id"], old["is_playing"]) != (new["id"], new["is_playing"]):
            return True
        # the UI interpolates progress; only resync if it drifted (seek / skipped around)
        elapsed = (time.time() - old["_t"]) * 1000 if old["is_playing"] else 0
        return abs(old["progress_ms"] + elapsed - new["progress_ms"]) > 2500

    async def poll_once(self) -> float:
        """One poll. Returns seconds to sleep before the next one."""
        r = await self._request("GET", "/me/player/currently-playing?additional_types=episode")
        if r.status_code == 429:
            return float(r.headers.get("Retry-After", 10)) + 1
        if r.status_code == 204:
            new = None
        elif r.status_code == 200:
            new = self.parse(r.json())
        else:
            log.warning("spotify HTTP %s", r.status_code)
            return max(self.poll, 10)
        if self._changed(new):
            if new:
                new["_t"] = time.time()
            self._last = new
            await self.hub.update(
                "spotify", None if new is None else {k: v for k, v in new.items() if k != "_t"}
            )
        return self.poll

    async def run(self) -> None:
        if not self.configured:
            log.warning("Spotify enabled but SPOTIFY_* credentials are missing; skipping")
            return
        delay = self.poll
        while True:
            try:
                delay = await self.poll_once()
            except Exception as e:  # network blip etc: keep the loop alive
                log.warning("spotify poll failed: %s", e)
                delay = max(self.poll, 10)
            await asyncio.sleep(delay)

    async def control(self, action: str) -> int:
        routes = {
            "play": ("PUT", "/me/player/play"),
            "pause": ("PUT", "/me/player/pause"),
            "next": ("POST", "/me/player/next"),
            "previous": ("POST", "/me/player/previous"),
        }
        method, path = routes[action]
        r = await self._request(method, path)
        # re-poll soon so the UI reflects the change
        asyncio.create_task(self._soon())
        return r.status_code

    async def _soon(self) -> None:
        await asyncio.sleep(0.6)
        try:
            await self.poll_once()
        except Exception:
            pass
