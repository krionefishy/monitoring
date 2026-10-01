import argparse
import getpass
import secrets
import uuid

from sqlalchemy import select

from .auth import passwords
from .config import Settings
from .migrate import migrate
from .storage import Store, sessions, users


def main():
    parser = argparse.ArgumentParser(prog="monitor")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("rebuild-read")
    commands.add_parser("secret")
    for name in ("create-user", "set-password", "disable-user", "enable-user"):
        user = commands.add_parser(name)
        user.add_argument("username")
        if name == "create-user":
            user.add_argument("--role", choices=["admin", "viewer"], default="admin")
    for name in ("collector", "worker"):
        commands.add_parser(name)
    args = parser.parse_args()
    if args.command == "secret":
        print(secrets.token_urlsafe(48))
        return
    if args.command in ("collector", "worker"):
        from .worker import run

        run(args.command)
        return
    store = Store(Settings())
    try:
        if args.command == "rebuild-read":
            from .projection import rebuild

            migrate(store)
            rebuild(store)
            print("Read database rebuilt from primary aggregates.")
            return
        if args.command == "init":
            migrate(store)
            print("Monitoring v2 databases initialized. No users have been created automatically.")
            return
        store.validate_instance()
        username = args.username.strip()
        if not 1 <= len(username) <= 100:
            parser.error("username must contain 1–100 characters")
        password_hash = None
        if args.command in ("create-user", "set-password"):
            password = getpass.getpass("Password (12+ characters): ")
            if (
                len(password) < 12
                or len(password) > 256
                or password != getpass.getpass("Confirm password: ")
            ):
                parser.error("password must match and contain 12–256 characters")
            password_hash = passwords.hash(password)
        with store.primary.begin() as conn:
            existing = (
                conn.execute(select(users).where(users.c.username == username)).mappings().first()
            )
            if args.command == "create-user":
                if existing:
                    parser.error("user already exists")
                conn.execute(
                    users.insert().values(
                        id=str(uuid.uuid4()),
                        username=username,
                        password_hash=password_hash,
                        role=args.role,
                        enabled=True,
                    )
                )
            else:
                if not existing:
                    parser.error("user does not exist")
                values = (
                    {"password_hash": password_hash}
                    if password_hash
                    else {"enabled": args.command == "enable-user"}
                )
                conn.execute(users.update().where(users.c.id == existing["id"]).values(**values))
                conn.execute(sessions.delete().where(sessions.c.user_id == existing["id"]))
        print(f"{args.command}: {username}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
