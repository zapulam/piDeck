"""One-time helper to get a Spotify refresh token.

    python -m pideck.spotify_auth

1. In the Spotify dashboard, add the redirect URI from SPOTIFY_REDIRECT_URI
   (default http://127.0.0.1:8888/callback) to your app.
2. Run this, open the printed URL, approve, and paste the URL your browser lands on
   (the page itself won't load - that's fine, we only need the ?code=... in the address bar).
3. Put the printed SPOTIFY_REFRESH_TOKEN in .env.
"""
from __future__ import annotations

import base64
import os
import sys
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from dotenv import load_dotenv

from .sources.spotify import SCOPES, TOKEN_URL


def main() -> int:
    load_dotenv()
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    redirect = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    if not (cid and secret):
        print("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env first.")
        return 1
    url = "https://accounts.spotify.com/authorize?" + urlencode(
        {"client_id": cid, "response_type": "code", "redirect_uri": redirect, "scope": SCOPES}
    )
    print("Open this URL, approve access:\n\n" + url + "\n")
    pasted = input("Paste the full URL you were redirected to: ").strip()
    code = parse_qs(urlparse(pasted).query).get("code", [None])[0]
    if not code:
        print("No ?code= found in that URL.")
        return 1
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    r = httpx.post(
        TOKEN_URL,
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect},
        headers={"Authorization": f"Basic {basic}"},
    )
    if r.status_code != 200:
        print("Token exchange failed:", r.text)
        return 1
    print("\nAdd this to .env:\n\nSPOTIFY_REFRESH_TOKEN=" + r.json()["refresh_token"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
