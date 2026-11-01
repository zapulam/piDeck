import argparse
import logging
import os
import socket

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


def main() -> None:
    ap = argparse.ArgumentParser(prog="pideck", description="piDeck desk display server")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--mock", action="store_true", help="serve fake data (no accounts needed)")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config(a.config)
    app = create_app(a.config, mock=True if a.mock else None)
    port = a.port or cfg["server"]["port"]
    if not a.mock:
        tok = " --token YOUR_PIDECK_TOKEN" if os.environ.get("PIDECK_TOKEN") else ""
        print(f"\n  Show your Claude Code / Codex agents here: on the machine that runs them, run\n"
              f"    python3 hooks/install.py --url http://{lan_ip()}:{port}{tok} --test\n")
    uvicorn.run(app, host=a.host or cfg["server"]["host"], port=port)


if __name__ == "__main__":
    main()
