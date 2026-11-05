"""Local command-line access to private report receipts."""

import argparse
import json
from pathlib import Path

from reporting.receiver.storage import Store


def main():
    parser = argparse.ArgumentParser(description="Inspect and delete local Gamja reports")
    parser.add_argument("--db", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("count")
    commands.add_parser("prune")
    for name in ("inspect", "delete"):
        commands.add_parser(name).add_argument("receipt_id")
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("database does not exist")
    store = Store(args.db, prune_on_open=args.command != "prune")
    try:
        if args.command == "count":
            output = store.counts()
        elif args.command == "prune":
            output = {"deleted": store.prune()}
        elif args.command == "inspect":
            output = store.inspect(args.receipt_id)
            if output is None:
                parser.error("receipt not found")
        else:
            output = {"deleted": store.delete(args.receipt_id)}
        print(json.dumps(output, ensure_ascii=False))
    finally:
        store.close()


if __name__ == "__main__":
    main()
