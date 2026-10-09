"""Launch the hub: `python -m silong` serves the app on every interface."""

import sys

import uvicorn

from silong.app import create_app
from silong.config import ConfigError, load_config

HOST = "0.0.0.0"


def main() -> int:
    try:
        config = load_config()
    except ConfigError as error:
        print(f"silong: {error}", file=sys.stderr)
        return 2
    # One process with no reload. workers=1 stops uvicorn from reading WEB_CONCURRENCY,
    # which would make it refuse an app object and exit before serving.
    uvicorn.run(create_app(config), host=HOST, port=config.port, workers=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
