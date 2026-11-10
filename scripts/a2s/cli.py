"""CLI: python -m a2s <command> ...   (all commands print JSON to stdout)

  inventory <files...>                       tool inventory + complexity per workflow
  convert   <files...> -o DIR [--source-map map.json] [--slug-map slugs.json]
  synth     <files...> -o DIR [--rows N] [--source-map map.json] [--schema-json cols.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .emit import Project
from .mappers import sanitize
from .parse import inventory, parse_workflow


def _slug(wf, slugs: dict) -> str:
    return slugs.get(wf.name) or sanitize(wf.name)[:24].rstrip("_")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="a2s")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_inv = sub.add_parser("inventory")
    p_inv.add_argument("files", nargs="+")
    p_conv = sub.add_parser("convert")
    p_conv.add_argument("files", nargs="+")
    p_conv.add_argument("-o", "--out", required=True)
    p_conv.add_argument("--source-map", help="JSON: {'<workflow>#<toolid>': {database, schema, table, columns}}")
    p_conv.add_argument("--slug-map", help="JSON: {'<workflow name>': 'short_slug'}")
    p_conv.add_argument("--raw-schema", default="RAW_ALTERYX")
    p_conv.add_argument("--no-null-safe-joins", action="store_true",
                        help="use plain '=' join keys (faster; differs from Alteryx when keys are NULL)")
    for k in ("role", "warehouse", "database", "schema"):
        p_conv.add_argument(f"--target-{k}", help=f"profiles.yml {k} (dbt on Snowflake)")
    p_syn = sub.add_parser("synth")
    p_syn.add_argument("files", nargs="+")
    p_syn.add_argument("-o", "--out", required=True)
    p_syn.add_argument("--rows", type=int, default=500)
    p_syn.add_argument("--row-counts", help="JSON {TABLE: rows} (e.g. production row counts from metadata)")
    p_syn.add_argument("--source-map")
    p_syn.add_argument("--slug-map")
    p_val = sub.add_parser("validate", help="run semantics conformance + per-tool invariants in Snowflake via `snow sql`")
    p_val.add_argument("files", nargs="+")
    p_val.add_argument("-c", "--connection", required=True, help="snow CLI connection name")
    p_val.add_argument("--database", required=True)
    p_val.add_argument("--schema", required=True, help="schema the dbt models were built into")
    p_val.add_argument("--source-map")
    p_val.add_argument("--slug-map")
    p_val.add_argument("-o", "--out", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "inventory":
        res = []
        for f in a.files:
            try:
                res.append(inventory(parse_workflow(f)))
            except Exception as e:  # noqa: BLE001
                res.append({"file": f, "error": str(e)})
        print(json.dumps({"status": "ok", "workflows": res}, indent=2, default=str))
        return 0

    smap = json.loads(Path(a.source_map).read_text()) if getattr(a, "source_map", None) else {}
    slugs = json.loads(Path(a.slug_map).read_text()) if getattr(a, "slug_map", None) else {}

    if a.cmd == "convert":
        out = []
        target = {k: getattr(a, f"target_{k}") for k in ("role", "warehouse", "database", "schema") if getattr(a, f"target_{k}")}
        for f in a.files:
            wf = parse_workflow(f)
            proj = Project(wf, _slug(wf, slugs), smap, a.raw_schema, null_safe_joins=not a.no_null_safe_joins,
                           target=target).build()
            root = proj.write(a.out)
            r = proj.report()
            out.append({"workflow": wf.name, "project": str(root), "models": len(r["models"]),
                        "ctes": r["cte_count"], "issues": r["counts"],
                        "schema_check": f"{r['schema_check']['tools_matching']}/{r['schema_check']['tools_checked']}"})
        print(json.dumps({"status": "ok", "projects": out}, indent=2))
        return 0

    if a.cmd == "synth":
        from .synth import synth_project
        counts = json.loads(Path(a.row_counts).read_text()) if a.row_counts else {}
        out = []
        for f in a.files:
            wf = parse_workflow(f)
            proj = Project(wf, _slug(wf, slugs), smap).build()
            out.append(synth_project(proj, Path(a.out), a.rows, counts))
        print(json.dumps({"status": "ok", "synthetic": out}, indent=2))
        return 0

    if a.cmd == "validate":
        from .validate import run_validation
        res = run_validation([parse_workflow(f) for f in a.files], slugs, smap, a.connection,
                             a.database, a.schema, Path(a.out))
        print(json.dumps(res, indent=2, default=str))
        return 0 if res["status"] == "ok" else 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
