"""Local administrative commands; no public registration endpoint."""

import argparse
from getpass import getpass

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User
from app.security import hasher


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["create-user"])
    parser.add_argument("--username", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    password = getpass("Password (at least 12 characters): ")
    if len(password) < 12 or len(password) > 200:
        parser.error("Password must have 12–200 characters")
    with SessionLocal.begin() as db:
        if db.scalar(select(User).where(User.username == args.username)):
            parser.error("Username already exists")
        db.add(User(username=args.username, name=args.name, password_hash=hasher.hash(password)))
    print("User created")


if __name__ == "__main__":
    main()
