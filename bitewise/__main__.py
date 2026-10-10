import argparse
import sys

import uvicorn

from . import config, users


def main() -> None:
    parser = argparse.ArgumentParser(prog="bitewise")
    parser.add_argument("--host", default=config.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    commands = parser.add_subparsers(dest="command")
    add_user = commands.add_parser("add-user", help="create a user and print its access token")
    add_user.add_argument("name")
    args = parser.parse_args()

    if args.command == "add-user":
        _add_user(args.name)
        return

    # Fail fast with a readable message instead of a startup traceback.
    try:
        users.load()
    except users.UsersError as exc:
        sys.exit(f"bitewise: {exc}")
    uvicorn.run("bitewise.app:app", host=args.host, port=args.port)


def _add_user(name: str) -> None:
    try:
        token = users.add_user(name)
    except users.UsersError as exc:
        sys.exit(f"bitewise: {exc}")
    print(f"Added user {name!r} to {config.USERS_FILE}")
    print(f"Token (shown once, put it in the shortcuts and the login page):\n\n    {token}\n")
    print("Restart the server to pick up the new user.")


if __name__ == "__main__":
    main()
