"""Write a dbt project (Snowflake dbt-projects compatible) for one converted workflow."""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from .expr import lit, qid
from .mappers import Converter, Rel, cast_expr, sanitize, textinput_fields, textinput_rows
from .parse import Field, Workflow, inventory

SF_KIND = {"FLOAT": "num", "NUMBER": "num", "REAL": "num", "DOUBLE": "num", "INT": "num", "INTEGER": "num",
           "DECIMAL": "num", "FIXED": "num", "VARCHAR": "str", "TEXT": "str", "STRING": "str", "CHAR": "str",
           "DATE": "date", "TIMESTAMP_NTZ": "date", "TIMESTAMP_LTZ": "date", "TIMESTAMP_TZ": "date",
           "BOOLEAN": "bool"}


def _sf_kind(t: str) -> str:
    return SF_KIND.get(re.sub(r"\(.*", "", t.upper()), "str")


class Project:
    """Builds the dbt project file set in memory, then writes it."""

    def __init__(self, wf: Workflow, slug: str, source_map: dict, raw_schema: str = "RAW_ALTERYX",
                 profile: str = "alteryx_migration", null_safe_joins: bool = True, target: dict | None = None):
        self.wf, self.slug, self.smap = wf, slug, source_map
        self.raw_schema, self.profile = raw_schema, profile
        # dbt on Snowflake runs as the deploying user; role must be the role that runs the project
        self.target = {"role": "<role>", "warehouse": "<warehouse>", "database": "<database>",
                       "schema": "ALTERYX_MIGRATED", **(target or {})}
        self.files: dict[str, str] = {}
        self.raw_tables: list[dict] = []
        self.inputs: dict[str, dict] = {}
        self.inv = inventory(wf)
        rels = {}
        for t in wf.tools.values():
            if t.type == "Input":
                rels[t.id] = "{{ ref('%s') }}" % self.stg_name(t.id)
            elif t.type == "TextInput":
                rels[t.id] = "{{ ref('%s') }}" % self.stg_name(t.id)
        self.conv = Converter(wf, rels, null_safe_joins).run()

    # ------------------------------------------------------------------ names
    def tool_stub(self, tid: str) -> str:
        t = self.wf.tools[tid]
        if t.type == "Input":
            raw = (t.config.findtext("File") or "").strip()
            path, _, sheet = raw.partition("|||")
            stem = re.split(r"[\\/]", path)[-1].rsplit(".", 1)[0]
            return sanitize(stem + ("_" + sheet.strip("`$") if sheet else ""))[:40]
        if t.type == "TextInput":
            return "text_input"
        return sanitize(t.type)

    def stg_name(self, tid: str) -> str:
        return f"{self.slug}__stg_t{tid}_{self.tool_stub(tid)}"

    # ------------------------------------------------------------------ staging
    def build_staging(self):
        for t in self.wf.tools.values():
            if t.type == "Input":
                self._stg_input(t)
            elif t.type == "TextInput":
                self._stg_text(t)

    def _stg_input(self, t):
        raw = (t.config.findtext("File") or "").strip()
        path, _, sheet = raw.partition("|||")
        fields = t.meta.get("Output", [])
        fname_field = (t.config.find("File").get("OutputFileName") or "") if t.config.find("File") is not None else ""
        m = self.smap.get(f"{self.wf.name}#{t.id}") or self.smap.get(t.id)
        cols = []
        if m:
            src_name, table = "mapped", m["table"]
            native = {k.upper(): v for k, v in (m.get("columns") or {}).items()}
            entry = {"source": "mapped", "database": m.get("database"), "schema": m.get("schema"), "table": table}
        else:
            table = sanitize(self.tool_stub(t.id)).upper()
            native = {}
            entry = {"source": "raw", "database": None, "schema": self.raw_schema, "table": table}
            self.raw_tables.append({"tool": t.id, "table": table, "file": path, "sheet": sheet or None,
                                    "columns": [f.name for f in fields if f.name != fname_field]})
        for f in fields:
            if fname_field and f.name == fname_field:
                base = re.split(r"[\\/]", path)[-1].replace("'", "")
                cols.append(f"'{{{{ var('{self.slug}_t{t.id}_filename', '{base}') }}}}'::VARCHAR AS {qid(f.name)}")
                continue
            src_col = f.name.upper() if m and f.name.upper() in native else f.name
            from_kind = _sf_kind(native[src_col]) if m and src_col in native else "str"
            x = qid(src_col)
            if f.kind != from_kind:
                x = cast_expr(x, from_kind, f.atype)
            cols.append(f"{x} AS {qid(f.name)}" if x != qid(f.name) else x)
        entry.update({"tool": t.id, "file": path, "sheet": sheet or None, "model": self.stg_name(t.id),
                      "fields": [(f.name, f.atype) for f in fields]})
        self.inputs[t.id] = entry
        body = ",\n    ".join(cols)
        src = "{{ source('%s', '%s') }}" % (f"{self.slug}_{entry['source']}", table)
        self.files[f"models/staging/{self.stg_name(t.id)}.sql"] = (
            f"-- Alteryx Input tool {t.id}: {path}{(' [' + sheet + ']') if sheet else ''}\n"
            f"-- Casts every column to the type Alteryx read it as, so downstream logic behaves identically.\n"
            f"SELECT\n    {body}\nFROM {src}\n")

    def _stg_text(self, t):
        names, rows = textinput_rows(t)
        fields = textinput_fields(t)
        seed = f"{self.slug}__seed_t{t.id}"
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(names)
        w.writerows(rows)
        self.files[f"seeds/{seed}.csv"] = buf.getvalue()
        self._seed_cfg = getattr(self, "_seed_cfg", {})
        self._seed_cfg[seed] = {n: "varchar" for n in names}
        cols = []
        for f in fields:
            if f.kind == "str":
                cols.append(f"COALESCE({qid(f.name)}, '') AS {qid(f.name)}")  # Text Input blanks are empty strings
            elif f.kind == "num":
                cols.append(f"TRY_TO_DOUBLE({qid(f.name)}) AS {qid(f.name)}")
            elif f.kind == "bool":
                cols.append(f"TRY_TO_BOOLEAN(NULLIF({qid(f.name)}, '')) AS {qid(f.name)}")
            else:
                cols.append(f"TRY_TO_TIMESTAMP({qid(f.name)}) AS {qid(f.name)}")
        self.inputs[t.id] = {"source": "seed", "tool": t.id, "seed": seed, "model": self.stg_name(t.id),
                             "rows": len(rows), "fields": [(f.name, f.atype) for f in fields]}
        self.files[f"models/staging/{self.stg_name(t.id)}.sql"] = (
            f"-- Alteryx Text Input tool {t.id} ({len(rows)} rows) -> editable seed seeds/{seed}.csv\n"
            f"SELECT\n    " + ",\n    ".join(cols) + f"\nFROM {{{{ ref('{seed}') }}}}\n")

    # ------------------------------------------------------------------ marts
    def sinks(self) -> list[tuple[str, str, str, Rel]]:
        """(tool id, model name, kind, relation) for Output, Browse and dead-end tools."""
        out = []
        for tid in self.wf.topo_order():
            t = self.wf.tools[tid]
            has_children = bool(self.wf.outputs_of(tid))
            if t.type == "Output":
                raw = (t.config.findtext("File") or "").strip()
                stem = re.split(r"[\\/]", raw.split("|||")[0])[-1].rsplit(".", 1)[0]
                out.append((tid, f"{self.slug}__out_{sanitize(stem)}", "output", self.conv.out[(tid, "Output")]))
            elif t.type == "Browse":
                out.append((tid, f"{self.slug}__browse_t{tid}", "browse", self.conv.out[(tid, "Output")]))
            elif not has_children and t.type not in ("Input", "TextInput", "Test"):
                for (otid, anchor), rel in self.conv.out.items():
                    if otid == tid and anchor in ("Output", "Join", "True", "Unique"):
                        out.append((tid, f"{self.slug}__leaf_t{tid}_{sanitize(t.type)}", "leaf", rel))
                        break
        return out

    def needed(self, cte: str, acc: list[str]):
        for d in sorted(self.conv.cte_deps.get(cte, ()), key=list(self.conv.ctes).index):
            if d not in acc:
                self.needed(d, acc)
        if cte not in acc:
            acc.append(cte)

    def render_select(self, rel: Rel, header: str, materialized: str) -> str:
        order = []
        self.needed(rel.cte, order)
        order.sort(key=list(self.conv.ctes).index)
        parts = []
        for c in order:
            body = self.conv.ctes[c]
            if re.search(r"\{\{|\{%|\{#", body) and "ref(" not in body:
                body = "{% raw %}" + body + "{% endraw %}"
            tool = self.wf.tools[self.conv.cte_tool[c]]
            note = f"  -- {tool.type} #{tool.id}" + (f": {tool.annotation[:80]}" if tool.annotation else "")
            parts.append(f"{c} AS ({note}\n    {body}\n)")
        ob = ""
        if rel.order:
            ob = "\nORDER BY " + ", ".join(f"{qid(c)} {d} NULLS {'FIRST' if d == 'ASC' else 'LAST'}" for c, d in rel.order)
        cfg = "{{ config(materialized='%s') }}\n" % materialized
        return f"{cfg}{header}\nWITH\n" + ",\n".join(parts) + f"\nSELECT * FROM {rel.cte}{ob}\n"

    def build_marts(self):
        self.models = []
        for tid, name, kind, rel in self.sinks():
            t = self.wf.tools[tid]
            folder = {"output": "marts", "browse": "inspect", "leaf": "inspect"}[kind]
            mat = "table" if kind == "output" else "view"
            hdr = f"-- Alteryx {t.type} tool {tid}" + (f" -> {(t.config.findtext('File') or '').strip()}" if kind == "output" else "")
            self.files[f"models/{folder}/{name}.sql"] = self.render_select(rel, hdr, mat)
            self.models.append({"tool": tid, "model": name, "kind": kind, "columns": [f.name for f in rel.fields],
                                "types": [f.atype for f in rel.fields],
                                "order_by": rel.order})

    def build_tests(self):
        for i, tst in enumerate(self.conv.tests, 1):
            chk = f"{self.slug}__chk_t{tst['tool']}_{i}"
            self.files[f"models/checks/{chk}.sql"] = self.render_select(tst["rel"], f"-- Input to Alteryx Test tool {tst['tool']}: {tst['name']}", "ephemeral")
            body = tst["sql"].replace("{rel}", "{{ ref('%s') }}" % chk)
            self.files[f"tests/{self.slug}__test_t{tst['tool']}_{i}.sql"] = f"-- Alteryx Test: {tst['name']}\n{body}\n"

    # ------------------------------------------------------------------ project files
    def build_project(self):
        p = self.slug
        srcs = {}
        for e in self.inputs.values():
            if e["source"] in ("raw", "mapped"):
                key = f"{p}_{e['source']}"
                s = srcs.setdefault(key, {"name": key, "tables": [],
                                          "database": e.get("database"), "schema": e.get("schema")})
                if e["table"] not in [x["name"] for x in s["tables"]]:
                    s["tables"].append({"name": e["table"], "description": f"Alteryx input {e['file']}",
                                        "quoting": {"identifier": False}})
        lines = ["version: 2", "sources:"]
        for s in srcs.values():
            db_var = f"{p}_{s['name'].split('_')[-1]}_database"
            sc_var = f"{p}_{s['name'].split('_')[-1]}_schema"
            lines += [f"  - name: {s['name']}",
                      f"    database: \"{{{{ var('{db_var}', target.database) }}}}\"",
                      f"    schema: \"{{{{ var('{sc_var}', '{s['schema']}') }}}}\"",
                      "    quoting:", "      identifier: false", "    tables:"]
            for tb in s["tables"]:
                lines += [f"      - name: {tb['name']}", f"        description: \"{tb['description'].replace(chr(92), '/')}\""]
        self.files["models/sources.yml"] = "\n".join(lines) + "\n"
        seed_cfg = getattr(self, "_seed_cfg", {})
        sl = ["version: 2", "seeds:"]
        for name, cols in seed_cfg.items():
            sl += [f"  - name: {name}", "    config:", "      quote_columns: true", "      column_types:"]
            sl += [f"        \"{c}\": varchar" for c in cols]
        self.files["seeds/seeds.yml"] = "\n".join(sl) + "\n" if seed_cfg else "version: 2\n"
        vars_ = {}
        for e in self.inputs.values():
            if e["source"] == "mapped":
                vars_[f"{p}_mapped_database"] = e["database"]
                vars_[f"{p}_mapped_schema"] = e["schema"]
        vlines = "".join(f"  {k}: {v}\n" for k, v in vars_.items())
        self.files["dbt_project.yml"] = (
            f"name: {p}\nversion: '1.0.0'\nconfig-version: 2\nprofile: {self.profile}\n"
            "model-paths: ['models']\nseed-paths: ['seeds']\ntest-paths: ['tests']\n"
            # relation names unquoted -> UPPERCASE objects; column names are quoted inside the SQL
            "quoting:\n  database: false\n  schema: false\n  identifier: false\n"
            f"models:\n  {p}:\n    staging:\n      +materialized: view\n    inspect:\n      +materialized: view\n"
            f"    marts:\n      +materialized: table\n"
            + (f"vars:\n{vlines}" if vlines else ""))
        tg = self.target
        self.files["profiles.yml"] = (
            f"{self.profile}:\n  target: dev\n  outputs:\n    dev:\n      type: snowflake\n"
            "      account: ''\n      user: ''\n"
            f"      role: {tg['role']}\n      warehouse: {tg['warehouse']}\n"
            f"      database: {tg['database']}\n      schema: {tg['schema']}\n      threads: 4\n")

    def build_setup(self):
        out = ["-- Landing tables for file-based Alteryx inputs (all VARCHAR; staging casts to Alteryx types).",
               f"CREATE SCHEMA IF NOT EXISTS {self.raw_schema};",
               f"CREATE STAGE IF NOT EXISTS {self.raw_schema}.ALTERYX_FILES;", ""]
        for r in self.raw_tables:
            cols = ",\n    ".join(f"{qid(c)} VARCHAR" for c in r["columns"])
            out.append(f"-- {r['file']}{(' [' + r['sheet'] + ']') if r['sheet'] else ''}")
            out.append(f"CREATE TABLE IF NOT EXISTS {self.raw_schema}.{r['table']} (\n    {cols}\n);")
            out.append(f"-- COPY INTO {self.raw_schema}.{r['table']} FROM @{self.raw_schema}.ALTERYX_FILES/{r['table'].lower()}.csv\n"
                       f"--   FILE_FORMAT = (TYPE = CSV PARSE_HEADER = TRUE FIELD_OPTIONALLY_ENCLOSED_BY = '\"')\n"
                       f"--   MATCH_BY_COLUMN_NAME = CASE_SENSITIVE;\n")
        self.files["setup/01_landing_tables.sql"] = "\n".join(out) + "\n"
        exp = ["-- Egress: write each output model to a stage as the file Adaptive Planning imports today.",
               "-- Confirm the import format Adaptive expects (CSV vs XLSX) before scheduling.", ""]
        for m in self.models:
            if m["kind"] != "output":
                continue
            ob = ""
            if m["order_by"]:
                ob = " ORDER BY " + ", ".join(f"{qid(c)} {d}" for c, d in m["order_by"])
            exp.append(f"COPY INTO @{self.raw_schema}.ALTERYX_FILES/exports/{m['model']}.csv\n"
                       f"  FROM (SELECT * FROM {m['model'].upper()}{ob})\n"
                       "  FILE_FORMAT = (TYPE = CSV COMPRESSION = NONE FIELD_OPTIONALLY_ENCLOSED_BY = '\"' NULL_IF = ())\n"
                       "  HEADER = TRUE SINGLE = TRUE OVERWRITE = TRUE MAX_FILE_SIZE = 268435456;\n")
        self.files["setup/02_exports.sql"] = "\n".join(exp) + "\n"
        self.files["setup/03_schedule.sql"] = (
            "-- Replace the Alteryx Server schedule: run the dbt project, then the exports.\n"
            f"CREATE OR REPLACE TASK {self.slug.upper()}_RUN\n  WAREHOUSE = <warehouse>\n  SCHEDULE = 'USING CRON 0 6 * * * America/Los_Angeles'\nAS\n"
            f"  EXECUTE DBT PROJECT <db>.<schema>.{self.slug.upper()} ARGS = 'build';\n")

    def report(self) -> dict:
        issues = [{"tool": i.tool, "severity": i.severity, "message": i.message} for i in self.conv.issues]
        sc = self.conv.schema_checks
        return {
            "workflow": self.wf.name, "slug": self.slug, "inventory": self.inv,
            "inputs": list(self.inputs.values()), "models": self.models,
            "tests": [{"tool": x["tool"], "name": x["name"]} for x in self.conv.tests],
            "issues": issues,
            "counts": {s: sum(1 for i in issues if i["severity"] == s) for s in ("TODO", "WARN", "NOTE")},
            "schema_check": {"tools_checked": len(sc),
                             "tools_matching": sum(1 for s in sc if s["names_match"] and not s["type_mismatches"]),
                             "mismatches": [s for s in sc if not s["names_match"] or s["type_mismatches"]]},
            "cte_count": len(self.conv.ctes),
            "lineage": {c: self.conv.cte_tool[c] for c in self.conv.ctes},
        }

    def build_parity(self):
        from .validate import parity_sql
        db, sc = self.target["database"], self.target["schema"]
        kinds = {"output"} if any(m["kind"] == "output" for m in self.models) else {"browse", "leaf"}
        for m in self.models:
            if m["kind"] in kinds:
                base = f"{db}.ALTERYX_BASELINE.{m['model'].upper()}"
                self.files[f"parity/{m['model']}.sql"] = parity_sql(self, m, base, db, sc)

    def build(self) -> "Project":
        self.build_staging()
        self.build_marts()
        self.build_tests()
        self.build_project()
        self.build_setup()
        self.build_parity()
        rep = self.report()
        self.files["conversion_report.json"] = json.dumps(rep, indent=2, default=str)
        self.files["CONVERSION.md"] = conversion_md(rep)
        return self

    def write(self, out_dir: str | Path) -> Path:
        root = Path(out_dir) / self.slug
        for rel, content in self.files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return root


def conversion_md(r: dict) -> str:
    inv = r["inventory"]
    lines = [f"# {r['workflow']} -> dbt project `{r['slug']}`", "",
             f"- Tools: {inv['tool_count']} ({inv['complexity']}, score {inv['complexity_score']})",
             f"- Generated CTEs: {r['cte_count']}",
             f"- Schema check vs Alteryx cached metadata: {r['schema_check']['tools_matching']}/{r['schema_check']['tools_checked']} tools match",
             f"- Issues: {r['counts']}", "", "## Inputs", "", "| Tool | Source | Model |", "|---|---|---|"]
    for i in r["inputs"]:
        src = i.get("file") or f"Text Input ({i.get('rows')} rows)"
        tgt = f"{i.get('database') or ''}.{i.get('schema') or ''}.{i.get('table') or i.get('seed')}".strip(".")
        lines.append(f"| {i['tool']} | {src} -> `{tgt}` | `{i['model']}` |")
    lines += ["", "## Outputs", "", "| Tool | Kind | Model | Columns |", "|---|---|---|---|"]
    for m in r["models"]:
        lines.append(f"| {m['tool']} | {m['kind']} | `{m['model']}` | {len(m['columns'])} |")
    if r["tests"]:
        lines += ["", "## Data tests (from Alteryx Test tools)", ""] + [f"- Tool {t['tool']}: {t['name']}" for t in r["tests"]]
    lines += ["", "## Issues", "", "| Severity | Tool | Message |", "|---|---|---|"]
    for i in sorted(r["issues"], key=lambda x: ("TODO", "WARN", "NOTE").index(x["severity"])):
        lines.append(f"| {i['severity']} | {i['tool']} | {i['message'].replace('|', '/')} |")
    return "\n".join(lines) + "\n"
