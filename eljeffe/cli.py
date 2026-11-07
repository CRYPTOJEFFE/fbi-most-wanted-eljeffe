"""Portable command-line interface, with no third-party runtime dependencies."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .service import Workspace, default_data_dir


def json_print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def add_filters(parser):
    parser.add_argument("--source", default="", help="Exact source ID shown by 'sources'")
    parser.add_argument("--kind", choices=["wanted", "sanction", "pep", "missing", "other"], default="")
    parser.add_argument("--schema", default="", help="FtM type, e.g. Person, Company or Sanction")
    parser.add_argument("--include-inactive", action="store_true", help="Include records absent from a later complete snapshot")


def build_parser():
    parser = argparse.ArgumentParser(prog="eljeffe", description="EL JEFFE • local FBI / OpenSanctions records and FollowTheMoney exports")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(), help="Local data folder (or ELJEFFE_DATA_DIR)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create or inspect the local workspace")
    sync = commands.add_parser("sync", help="Fetch a current official source snapshot")
    sync.add_argument("source", choices=["fbi", "opensanctions"])
    sync.add_argument("--dataset", default="us_fbi_most_wanted", help="OpenSanctions dataset ID, e.g. us_ofac_sdn")
    sync.add_argument("--max-pages", type=int, help="FBI page cap; capped fetches do not deactivate unseen records")
    sync.add_argument("--json", action="store_true")
    discover = commands.add_parser("discover", help="Discover an OpenSanctions dataset page and its official files")
    discover.add_argument("dataset", nargs="?", default="us_fbi_most_wanted")
    imp = commands.add_parser("import", help="Import a local flat FtM JSONL file (optionally .gz)")
    imp.add_argument("file", type=Path)
    imp.add_argument("--source", default=None, help="Stable source ID for replacing subsequent snapshots")
    validate = commands.add_parser("validate", help="Check FtM JSON structure and bundled schema/property names")
    validate.add_argument("file", type=Path)
    search = commands.add_parser("search", help="Search names, aliases, IDs and properties locally")
    search.add_argument("query", nargs="?", default="")
    add_filters(search)
    search.add_argument("--limit", type=int, default=25)
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--json", action="store_true")
    show = commands.add_parser("show", help="Show a record, provenance and FtM relationships")
    show.add_argument("id")
    show.add_argument("--source", default="")
    export = commands.add_parser("export", help="Export FtM JSONL and an attribution/checksum manifest")
    export.add_argument("--output", "-o", type=Path, required=True)
    export.add_argument("--query", default="")
    add_filters(export)
    files = commands.add_parser("files", help="Download available official FBI images and PDFs for one record")
    files.add_argument("id")
    files.add_argument("--source", default="")
    commands.add_parser("sources", help="Show local dataset provenance, counts and freshness")
    commands.add_parser("stats", help="Show local counts by source, schema and record type")
    commands.add_parser("history", help="Show completed local import history")
    commands.add_parser("demo", help="Explicitly load five fictional demo entities")
    serve = commands.add_parser("serve", help="Open the local Chrome/Chromium-compatible GUI")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true", help="Open the default browser")
    return parser


def main(argv=None):
    # Machine-readable output must remain UTF-8 when redirected on Windows.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "discover":
            from .sources import discover_dataset
            json_print(discover_dataset(args.dataset))
            return 0
        if args.command == "validate":
            from .model import model, schema_errors
            from .sources import read_ftm_file
            snapshot = read_ftm_file(args.file)
            count, errors = 0, []
            for entity in snapshot.entities:
                count += 1
                errors.extend({"id": entity["id"], "error": error} for error in schema_errors(entity))
            json_print({"records": count, "model_version": model()["version"], "errors": errors,
                        "scope": "JSON structure, schema names and property names; not full value normalization"})
            return 2 if errors else 0
        workspace = Workspace(args.data_dir)
        def progress(message):
            print(str(message), file=sys.stderr, flush=True)

        if args.command == "init":
            json_print({"data_dir": str(workspace.path), "version": __version__, **workspace.store.stats()})
        elif args.command == "sync":
            if args.max_pages is not None and args.max_pages < 1:
                raise ValueError("--max-pages must be positive")
            result = workspace.sync(args.source, args.dataset, max_pages=args.max_pages, progress=progress)
            if args.json:
                json_print(result)
            else:
                print(f"Imported {result['inserted']} entities from {result['source']}.")
                print("Snapshot complete." if result["complete"] else "Partial snapshot: unseen records were preserved.")
                print(f"Data folder: {workspace.path}")
        elif args.command == "import":
            json_print(workspace.import_file(args.file, source=args.source))
        elif args.command in ("search", "export"):
            filters = {"q": args.query, "source": args.source, "kind": args.kind,
                       "schema": args.schema, "inactive": args.include_inactive}
            if args.command == "export":
                json_print(workspace.export_file(args.output, **filters))
            else:
                result = workspace.store.search(**filters, limit=args.limit, offset=args.offset)
                if args.json:
                    json_print(result)
                else:
                    print(f"{result['total']} record(s) • offset {result['offset']} • shown {len(result['items'])}")
                    for item in result["items"]:
                        status = "in snapshot" if item["active"] else "absent from latest snapshot"
                        print(f"\n{item['caption']}\n  {item['kind']} / {item['schema']} / {status}")
                        print(f"  ID: {item['id']}\n  Source: {item['source']}")
        elif args.command == "show":
            result = workspace.store.get(args.id, args.source)
            if result is None:
                print("Record not found.", file=sys.stderr)
                return 1
            json_print(result)
        elif args.command == "files":
            json_print(workspace.fetch_files(args.id, args.source, progress))
        elif args.command == "sources":
            json_print(workspace.store.stats()["sources"])
        elif args.command == "stats":
            json_print(workspace.store.stats())
        elif args.command == "history":
            json_print(workspace.store.history())
        elif args.command == "demo":
            json_print(workspace.demo())
        elif args.command == "serve":
            from .server import serve
            serve(workspace, port=args.port, open_browser=args.open)
        return 0
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
