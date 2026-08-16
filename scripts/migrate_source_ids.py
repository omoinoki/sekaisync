"""One-time migration: rewrite legacy source IDs in a store.

Legacy IDs from releases before the multi-site rename:

  sekai_viewer          -> altsource_sv
  sekai_viewer_i18n     -> altsource_sv_i18n
  altsource             -> altsource_ms
  altsource_translation -> altsource_ms_translation

Renames the web page directories, rewrites page/news records, patches
TOS consent keys and rebuilds the regenerable web indexes in place.

Usage:
  python scripts/migrate_source_ids.py --store .\\store
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sekaisync.source_migrate import rename_legacy_source_ids  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--store",
        type=Path,
        default=Path("store"),
        help="Store root to migrate (default: ./store)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without modifying the store",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Snapshot the store to <store>.bak-<timestamp> before migrating",
    )
    args = parser.parse_args()
    store = args.store
    if not store.exists():
        print(f"Store not found: {store}", file=sys.stderr)
        return 1
    if args.backup and not args.dry_run:
        import shutil
        from datetime import datetime

        backup = store.with_name(store.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copytree(store, backup)
        print(f"Backed up store to {backup}", file=sys.stderr)
    result = rename_legacy_source_ids(store, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
