import argparse

import uvicorn

from . import config


def main() -> None:
    parser = argparse.ArgumentParser(prog="bitewise")
    parser.add_argument("--host", default=config.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    args = parser.parse_args()
    uvicorn.run("bitewise.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
