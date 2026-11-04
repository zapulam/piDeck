import argparse
import logging
import os
import secrets
import socket
import sys
from pathlib import Path

import uvicorn

from .config import load_config
from .main import create_app


def lan_ip() -> str:
    """Best guess at this machine's address on the local network (no traffic is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("10.255.255.255", 1))
            return sk.getsockname()[0]
    except OSError:
        return "localhost"


def ensure_token(config_path: str) -> tuple[str, bool]:
    """Return (token, generated). A shared secret is created on first run and saved to .env, so the
    server is never open to the whole network by accident."""
    tok = os.environ.get("PIDECK_TOKEN", "")
    if tok:
        return tok, False
    tok = secrets.token_urlsafe(24)
    env = Path(config_path).resolve().parent / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    lines = [l for l in lines if not l.strip().startswith("PIDECK_TOKEN=")] + [f"PIDECK_TOKEN={tok}"]
    env.write_text("\n".join(lines) + "\n")
    try:
        os.chmod(env, 0o600)
    except OSError:
        pass
    os.environ["PIDECK_TOKEN"] = tok
    return tok, True


def main() -> None:
    ap = argparse.ArgumentParser(prog="pideck", description="piDeck desk display server")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--mock", action="store_true", help="serve fake data (no accounts needed)")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config(a.config)
    generated = False
    if not a.mock:
        _, generated = ensure_token(a.config)
    app = create_app(a.config, mock=True if a.mock else None, prime=not a.mock)
    port = a.port or cfg["server"]["port"]
    if not a.mock:
        tok = os.environ["PIDECK_TOKEN"]
        shown = tok if sys.stdout.isatty() else "YOUR_PIDECK_TOKEN"   # keep the secret out of service logs
        print(f"\n  Show your Claude Code / Codex agents here: on the machine that runs them, run\n"
              f"    python3 hooks/install.py --url http://{lan_ip()}:{port} --token {shown} --test\n"
              + (f"  (a new access token was created and saved to {Path(a.config).resolve().parent / '.env'})\n" if generated else
                 "  (the token is PIDECK_TOKEN in your .env)\n"))
    uvicorn.run(app, host=a.host or cfg["server"]["host"], port=port)


if __name__ == "__main__":
    main()
