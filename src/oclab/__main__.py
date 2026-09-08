import argparse
import logging

import uvicorn

from .app import create_app
from .config import load_settings


def main():
    parser = argparse.ArgumentParser(description="Record and publish the OC Lab laser link")
    parser.add_argument("--config", help="Path to TOML configuration")
    parser.add_argument("--mode", choices=("hardware", "demo"))
    parser.add_argument("--data-dir")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings(args.config, mode=args.mode, data_dir=args.data_dir, host=args.host, port=args.port)
    except (ValueError, TypeError, OSError) as exc:
        parser.error(str(exc))
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, workers=1,
                timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
