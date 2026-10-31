import argparse
import logging

import uvicorn

from .config import load_config
from .main import create_app


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
    uvicorn.run(app, host=a.host or cfg["server"]["host"], port=a.port or cfg["server"]["port"])


if __name__ == "__main__":
    main()
