"""Manage user accounts from the host. The first account can also be registered in the
browser, and its owner registers the others there; this resets and removes them.

    python -m app.auth.users add alice@example.com   # also resets an existing password
    python -m app.auth.users remove alice@example.com
    python -m app.auth.users list
"""

import argparse
import asyncio
import getpass
import sys

from app.asyncio_utils import run_sync
from app.auth import sessions, storage
from app.auth.passwords import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, hash_password


def _read_password() -> str:
    # Piped input lets a deployment script supply the password without a terminal.
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    password = getpass.getpass("Password: ")
    if getpass.getpass("Repeat password: ") != password:
        raise SystemExit("The passwords do not match.")
    return password


async def _run(args: argparse.Namespace) -> None:
    await storage.ensure_schema()
    await sessions.ensure_schema()
    if args.command == "list":
        for user in await storage.list_users():
            print(user["login"])
        return
    try:
        login = storage.normalize_login(args.login)
    except storage.InvalidLogin as exc:
        raise SystemExit(str(exc)) from None
    if args.command == "remove":
        if not await storage.remove_user(login):
            raise SystemExit(f"No user {login}.")
        print(f"Removed {login} and ended their sessions.")
        return
    password = await run_sync(_read_password)
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise SystemExit(f"Use a password of {MIN_PASSWORD_LENGTH} to "
                         f"{MAX_PASSWORD_LENGTH} characters.")
    created = await storage.save_user(login, await run_sync(hash_password, password))
    if not created:
        # A new password signs the user out everywhere.
        await sessions.revoke_user_sessions(login)
    print(f"Created {login}." if created else
          f"Replaced the password of {login} and ended their sessions.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.auth.users", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("add", "remove"):
        commands.add_parser(name).add_argument("login", help="username or email address")
    commands.add_parser("list")
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
