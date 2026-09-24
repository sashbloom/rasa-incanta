"""Command line entry points. From the repo root:

    python -m api.cli run                  # pull Zoho, cards for every deal, one NBA
    python -m api.cli run --deal 12345     # draft the NBA for this Zoho deal id
    python -m api.cli run --limit 3        # draft NBAs for the top three deals

    python -m api.cli add-user --username mahak --role admin --sbu India --sbu USA \\
        --ms-email mahak@roibypractus.com  # prompts for a password
    python -m api.cli add-user --username rao --sbu India --no-password   # portal only
"""
import argparse
import getpass
import json
import logging
import sys

from sqlalchemy import select

from api.auth import hash_password
from api.config import get_settings
from api.db import get_sessionmaker
from api.engine.run import run_week
from api.models import UNUSABLE_PASSWORD, User, UserAllowedSbu


def add_user(args: argparse.Namespace) -> int:
    username = args.username.strip().lower()
    password_hash = UNUSABLE_PASSWORD
    if not args.no_password:
        password = sys.stdin.readline().rstrip("\n") if args.password_stdin else getpass.getpass("Password: ")
        if len(password) < 12:
            print("Use a password of at least 12 characters.", file=sys.stderr)
            return 2
        password_hash = hash_password(password)
    with get_sessionmaker()() as session:
        if session.scalar(select(User).where(User.username == username)):
            print(f"User {username} already exists.", file=sys.stderr)
            return 2
        user = User(username=username, password_hash=password_hash, role=args.role, ms_email=args.ms_email)
        user.allowed_sbus = [UserAllowedSbu(sbu=s.strip()) for s in dict.fromkeys(args.sbu or []) if s.strip()]
        session.add(user)
        session.commit()
        sbus = ", ".join(a.sbu for a in user.allowed_sbus) or "none (sees no deals)"
        print(f"Created {username} ({args.role}). SBUs: {sbus}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m api.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run this week's pass now")
    run.add_argument("--deal", help="Zoho deal id to draft the NBA for")
    run.add_argument("--limit", type=int, default=1, help="How many deals get an NBA (default 1)")
    user = sub.add_parser("add-user", help="Create a user who can sign in or arrive through the portal")
    user.add_argument("--username", required=True)
    user.add_argument("--role", choices=["super_admin", "admin", "user"], default="user")
    user.add_argument("--ms-email", help="Microsoft address used for the Practus Portal")
    user.add_argument("--sbu", action="append", help="An SBU this user may see; repeat for more")
    user.add_argument("--no-password", action="store_true", help="Portal only, no password sign-in")
    user.add_argument("--password-stdin", action="store_true", help="Read the password from stdin")
    args = parser.parse_args(argv)

    if args.command == "add-user":
        return add_user(args)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with get_sessionmaker()() as session:
        result = run_week(session, get_settings(), deal_zoho_id=args.deal, nba_limit=args.limit)
        print(json.dumps({"run": str(result.id), "status": result.status, "error": result.error,
                          "stats": result.stats}, indent=2, default=str))
        return 0 if result.status in ("succeeded", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
