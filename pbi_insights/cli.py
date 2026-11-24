"""Command line: python -m pbi_insights.cli <file.pbix> [--out outputs] [--dialect auto] [--data]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pipeline import DIALECTS, STEPS, Options, run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pbi_insights", description="Inventory a Power BI file for migration and "
                                 "write an Excel workbook (plus a JSON export).")
    ap.add_argument("file", help=".pbix, .pbit or .bim file")
    ap.add_argument("--out", default="outputs", help="output folder (default: outputs)")
    ap.add_argument("--dialect", default="auto", choices=sorted(DIALECTS), help="SQL dialect for generated SQL")
    ap.add_argument("--data", action="store_true", help="profile the saved data and test-run every insight query")
    ap.add_argument("--view-prefix", default="vw_", help="prefix for target view names (default: vw_)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    path = Path(args.file)
    if not path.is_file():
        print(f"File not found: {path}", file=sys.stderr)
        return 2

    def progress(i, msg):
        if not args.quiet:
            print(f"[{i + 1}/{len(STEPS)}] {msg}", flush=True)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    res = run(path, args.out, Options(dialect=args.dialect, read_data=args.data,
                                      view_prefix=args.view_prefix), progress)
    s = res.summary
    print(f"\n{s['tabs']} tabs, {s['data_insights']} data insights, {s['tables']} tables, {s['measures']} measures "
          f"({s['measures_translated']} translated, {s['measures_review']} to review), {s['findings']} findings "
          f"({s['findings_high']} high) in {s['seconds']} s")
    for w in res.warnings:
        print(f"WARNING: {w}")
    print(f"Workbook: {res.workbook}\nJSON:     {res.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
