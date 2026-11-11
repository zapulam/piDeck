"""

Configure and launch the piDeck HTTP server from the command line.

"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import socket
import sys
from pathlib import Path

import uvicorn
from dotenv import dotenv_values
from hooks.pideck_hook import private_write, protect_file

from .config import load_config
from .main import create_app
from .security import redact_logs
from .credentials import SecretStore


# --- Network and access-token helpers ---

def lan_ip() -> str:
    """
    Best guess at this machine's address on the local network (no traffic is sent).

    Returns:
        str: Local network IPv4 address, or localhost when no address can be detected.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("10.255.255.255", 1))
            return sk.getsockname()[0]
    except OSError:
        return "localhost"


def ensure_token(
    config_path: str,
    name: str = "PIDECK_TOKEN",
    ) -> tuple[str, bool]:
    """
    Return (token, generated). A shared secret is created on first run and saved to .env, so the
    server is never open to the whole network by accident.

    Args:
        config_path (str): Configuration file whose adjacent .env stores a newly generated token.
        name (str): Display or event-only credential environment variable to initialize.

    Returns:
        tuple[str, bool]: Access token and whether this call generated and saved it.
    """
    env = Path(config_path).resolve().parent / ".env"
    store = SecretStore(Path(config_path))
    store.load()
    if env.exists():
        protect_file(env)
    tok = os.environ.get(name, "")
    marker = env.parent / ".security-v2"
    migrated = marker.is_file()
    retired = ""
    if migrated and name == "PIDECK_TOKEN":
        try:
            retired = json.loads(marker.read_text(encoding="utf-8")).get("retired", "")
        except (ValueError, AttributeError):
            pass
        if tok and retired and hashlib.sha256(tok.encode()).hexdigest() == retired:
            saved = store.vault.get_password(store.service, name) if store.vault else dotenv_values(env).get(name, "")
            tok = saved if saved and hashlib.sha256(saved.encode()).hexdigest() != retired else ""
            os.environ[name] = tok
    if tok and (name != "PIDECK_TOKEN" or migrated):
        return tok, False
    if tok and name == "PIDECK_TOKEN":
        retired = hashlib.sha256(tok.encode()).hexdigest()
    tok = secrets.token_urlsafe(24)
    store.set(name, tok)
    if name == "PIDECK_TOKEN":
        private_write(marker, json.dumps({"version": 2, "retired": retired}) + "\n")
    return tok, True


# --- Server entry point ---


def main() -> None:
    """
    Parse server options, initialize configuration, and launch piDeck.

    Returns:
        None: No value is returned.
    """
    ap = argparse.ArgumentParser(prog="pideck", description="piDeck desk display server")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--mock", action="store_true", help="serve fake data (no accounts needed)")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--tls-cert", help="TLS certificate chain for direct HTTPS access")
    ap.add_argument("--tls-key", help="private key for direct HTTPS access")
    ap.add_argument("--no-prime", action="store_true", help="disable local hook replay and provider measurement reads")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config(a.config)
    redact_logs()
    generated = False
    if not a.mock:
        _, generated = ensure_token(a.config)
        ensure_token(a.config, "PIDECK_HOOK_TOKEN")
    app = create_app(a.config, mock=True if a.mock else None, prime=not (a.mock or a.no_prime))
    port = a.port or cfg["server"]["port"]
    if not a.mock:
        shown = "YOUR_PIDECK_HOOK_TOKEN"
        print(f"\n  Show your Claude Code / Codex agents here: on the machine that runs them, run\n"
              f"    python3 hooks/install.py --url https://YOUR_PI_HTTPS_ADDRESS --token {shown} --test\n"
              + (f"  (a new access token was created and saved to {Path(a.config).resolve().parent / '.env'})\n" if generated else
                 "  (event-only token: PIDECK_HOOK_TOKEN; display token: PIDECK_TOKEN in your private .env)\n"))
    from ipaddress import ip_address

    trusted = cfg["server"].get("trusted_proxy_ips", [])
    for address in trusted:
        ip_address(address)
    host = a.host or cfg["server"]["host"]
    if host not in {"127.0.0.1", "::1", "localhost"} and not (a.tls_cert and a.tls_key):
        ap.error("Network listeners require --tls-cert and --tls-key; HTTPS proxies should forward to loopback")
    uvicorn.run(app, host=host, port=port, ssl_certfile=a.tls_cert, ssl_keyfile=a.tls_key,
                proxy_headers=bool(trusted), forwarded_allow_ips=",".join(trusted),
                ws_max_size=1024, limit_concurrency=64, timeout_keep_alive=5)


if __name__ == "__main__":
    main()
