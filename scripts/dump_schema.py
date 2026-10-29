"""Dump a source's introspected schema to JSON (offline fixtures, debugging, eval snapshots).

Usage (PowerShell):
    python -m scripts.dump_schema --source wealthlens --out tests/fixtures/wealthlens_schema.json
    python -m scripts.dump_schema --source wealthlens --text      # print the prompt-ready schema text

Needs the source's read-only URL (WEALTHLENS_READONLY_URL) in the environment / .env.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from text2sql.config.sources import load_source
from text2sql.connectors.registry import get_connector
from text2sql.schema.introspect import introspect_connector
from text2sql.schema.render import render_schema


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Dump an introspected schema snapshot.")
    p.add_argument("--source", default="wealthlens")
    p.add_argument("--out", type=Path, help="Write the snapshot as JSON to this path")
    p.add_argument("--text", action="store_true", help="Print the schema as it appears in the prompt")
    p.add_argument("--no-samples", action="store_true", help="Skip sample values (structure only)")
    args = p.parse_args(argv)

    connector = get_connector(load_source(args.source))
    try:
        snapshot = introspect_connector(connector, with_samples=not args.no_samples)
    finally:
        connector.dispose()

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(snapshot.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"Wrote {len(snapshot.tables)} tables (fingerprint {snapshot.fingerprint}) to {args.out}")
    if args.text or not args.out:
        print(render_schema(snapshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
