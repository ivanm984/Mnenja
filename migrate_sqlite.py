"""Utility script to migrate legacy SQLite data into the configured database."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import DEFAULT_SQLITE_PATH
from app.database import DatabaseManager, migrate_sqlite_database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prenese vse shranjene analize in revizije iz lokalne SQLite baze v novo MySQL/PostgreSQL bazo."
        )
    )
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=DEFAULT_SQLITE_PATH,
        help="Pot do obstoječe SQLite baze (privzeto: %(default)s)",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help=(
            "Neposredni DSN do ciljne baze. Če ni podan, se uporabijo okoljske spremenljivke "
            "DATABASE_URL oz. MYSQL_/POSTGRES_."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    manager = DatabaseManager(database_url=args.database_url)
    try:
        result = migrate_sqlite_database(args.sqlite_path, manager)
    except FileNotFoundError as exc:
        parser.error(str(exc))
    print(
        "✅ Migracija uspešna: {sessions} sej in {revisions} revizij prenesenih.".format(
            sessions=result.get("sessions", 0),
            revisions=result.get("revisions", 0),
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI helper
    sys.exit(main())
