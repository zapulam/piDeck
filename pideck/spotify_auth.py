"""

Authorize Spotify with PKCE and validate callbacks without printing reusable secrets.

"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
from pathlib import Path
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from dotenv import load_dotenv

from .credentials import SecretStore
from .sources.spotify import SCOPES, TOKEN_URL


# --- PKCE authorization and callback validation ---


def authorization(
    client_id: str,
    redirect: str,
    ) -> tuple[str, str, str]:
    """
    Create an authorization URL tied to fresh OAuth state and a PKCE verifier.

    Args:
        client_id (str): Public Spotify application identifier.
        redirect (str): Exact registered callback URI.

    Returns:
        tuple[str, str, str]: Browser URL, expected state, and private PKCE verifier.
    """
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    url = "https://accounts.spotify.com/authorize?" + urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect, "scope": SCOPES,
        "state": state, "code_challenge_method": "S256", "code_challenge": challenge,
    })
    return url, state, verifier


def callback_code(
    callback: str,
    redirect: str,
    state: str,
    ) -> str:
    """
    Accept a code only from the exact callback URI and this setup attempt.

    Args:
        callback (str): Complete callback URI returned by the browser.
        redirect (str): Expected registered callback URI.
        state (str): Expected unpredictable OAuth state value.

    Returns:
        str: Authorization code, or a ValueError when validation fails.
    """
    actual, expected = urlsplit(callback), urlsplit(redirect)
    if ((actual.scheme, actual.netloc, actual.path) != (expected.scheme, expected.netloc, expected.path)
            or actual.username or actual.password or actual.fragment):
        raise ValueError("Unexpected Spotify callback address")
    query = parse_qs(actual.query, keep_blank_values=True)
    if (len(query.get("state", [])) != 1 or not hmac.compare_digest(query["state"][0], state)
            or len(query.get("code", [])) != 1 or not 0 < len(query["code"][0]) <= 4096):
        raise ValueError("Spotify setup expired or the callback did not match this request")
    return query["code"][0]


def exchange_code(
    code: str,
    client_id: str,
    redirect: str,
    verifier: str,
    ) -> str:
    """
    Exchange a validated code and PKCE verifier directly with Spotify.

    Args:
        code (str): Validated one-time authorization code.
        client_id (str): Public Spotify application identifier.
        redirect (str): Exact registered callback URI.
        verifier (str): Private PKCE verifier from this authorization attempt.

    Returns:
        str: Reusable refresh token, retained privately by the caller.
    """
    response = httpx.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code,
                                         "client_id": client_id, "redirect_uri": redirect,
                                         "code_verifier": verifier}, timeout=15, follow_redirects=False)
    if response.status_code != 200:
        raise ValueError("Spotify authorization failed; start a new connection attempt")
    token = response.json().get("refresh_token")
    if not isinstance(token, str) or not token:
        raise ValueError("Spotify did not provide a refresh token")
    return token


# --- Source and Pi authorization setup ---


def main(
    argv: list[str] | None = None,
    ) -> int:
    """
    Complete state-checked PKCE setup and save its refresh token privately.

    Args:
        argv (list[str] | None): Optional configuration arguments, or the process command line.

    Returns:
        int: Zero after private storage, or one when setup fails.
    """
    parser = argparse.ArgumentParser(description="Connect Spotify using PKCE")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    args = parser.parse_args(argv)
    load_dotenv(args.config.parent / ".env")
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    redirect = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    if not client_id:
        print("Add SPOTIFY_CLIENT_ID to the private configuration first.")
        return 1
    url, state, verifier = authorization(client_id, redirect)
    print("Open this Spotify authorization link:\n\n" + url)
    try:
        code = callback_code(input("Paste the full callback URL: ").strip(), redirect, state)
        token = exchange_code(code, client_id, redirect, verifier)
        SecretStore(args.config).set("SPOTIFY_REFRESH_TOKEN", token)
    except (ValueError, OSError, httpx.HTTPError):
        print("Spotify connection failed. Start again and use the callback from this attempt.")
        return 1
    print("Spotify connected. The refresh token was saved privately; restart piDeck.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
