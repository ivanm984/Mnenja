"""Populate the SQL knowledge base tables from JSON source files."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Any

from app.database import DatabaseManager
from app.knowledge_base import KNOWLEDGE_RESOURCE_FILES


def load_source_payloads(base_dir: Path) -> Dict[str, Any]:
    payloads: Dict[str, Any] = {}
    for key, filename in KNOWLEDGE_RESOURCE_FILES.items():
        path = base_dir / filename
        try:
            with path.open("r", encoding="utf-8") as handle:
                payloads[key] = json.load(handle)
        except FileNotFoundError:
            payloads[key] = {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"Neveljaven JSON v {path}: {exc}") from exc
    return payloads


def migrate(base_dir: Path, *, purge: bool = False) -> Dict[str, int]:
    manager = DatabaseManager()
    manager.init_db()
    if purge:
        manager.delete_all_knowledge_resources()

    payloads = load_source_payloads(base_dir)
    migrated = 0
    for name, payload in payloads.items():
        manager.upsert_knowledge_resource(name, payload)
        migrated += 1
    return {"resources": migrated}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migracija znanja iz JSON v SQL bazo")
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Mapa z izhodiščnimi JSON datotekami",
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="Pred uvozom izbriši obstoječe zapise",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = migrate(args.base_dir, purge=args.purge)
    print(
        "✅ Migriranih virov: {resources} (iz mape {base})".format(
            resources=result["resources"],
            base=args.base_dir,
        )
    )


if __name__ == "__main__":
    main()
