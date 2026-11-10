"""Validation: per-tool row-count invariants, semantics conformance, and Alteryx-vs-Snowflake parity SQL."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .expr import lit, qid, to_sql


def _snow(conn: str, sql_file: Path) -> list[dict]:
    """Run a SQL file with the Snowflake CLI and return the rows of the last result set."""
    p = subprocess.run(["snow", "sql", "-c", conn, "--enable-templating", "NONE", "-f", str(sql_file), "--format", "JSON"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stdout[-1500:] + p.stderr[-1500:])
    data = json.loads(p.stdout)
    if data and isinstance(data[0], list):
        data = data[-1]
    return data


def run_validation(workflows, slugs, smap, conn, database, schema, out: Path) -> dict:
    from .emit import Project
    from .mappers import sanitize
    out.mkdir(parents=True, exist_ok=True)
    report = {"status": "ok", "conformance": None, "workflows": {}}
    csql, _ = conformance_sql()
    (out / "conformance.sql").write_text(csql)
    rows = _snow(conn, out / "conformance.sql")
    conf = conformance_check({r["ID"]: r["GOT"] for r in rows})
    report["conformance"] = {"pass": sum(c["pass"] for c in conf), "total": len(conf),
                             "failed": [c for c in conf if not c["pass"]]}
    for wf in workflows:
        slug = slugs.get(wf.name) or sanitize(wf.name)[:24].rstrip("_")
        p = Project(wf, slug, smap).build()
        (out / f"probe_{slug}.sql").write_text(probe_sql(p, database, schema))
        counts = {r["CTE"]: r["N"] for r in _snow(conn, out / f"probe_{slug}.sql")}
        jm = {}
        js = join_match_sql(p, database, schema)
        if js:
            (out / f"joins_{slug}.sql").write_text(js)
            jm = {r["TOOL"]: (r["LEFT_MATCHED"], r["RIGHT_MATCHED"]) for r in _snow(conn, out / f"joins_{slug}.sql")}
        inv = invariants(p, counts, jm)
        tool_rows = {}
        for c, n in counts.items():
            tid = p.conv.cte_tool[c]
            tool_rows[tid] = max(tool_rows.get(tid, 0), n)
        gates = {t.id for t in wf.tools.values() if "should be 0" in t.annotation.lower()}
        report["workflows"][slug] = {
            "invariants_pass": sum(i["pass"] for i in inv), "invariants_total": len(inv),
            "failed": [i for i in inv if not i["pass"]],
            "tools_with_rows": sum(1 for n in tool_rows.values() if n > 0), "tools_total": len(tool_rows),
            "zero_row_tools": [{"tool": t, "type": wf.tools[t].type, "annotation": wf.tools[t].annotation[:80],
                                "reason": "data-quality gate (expected empty)" if t in gates else "no rows reached this tool"}
                               for t, n in tool_rows.items() if n == 0],
        }
        if inv and report["workflows"][slug]["failed"]:
            report["status"] = "invariant_failures"
    if report["conformance"]["failed"]:
        report["status"] = "conformance_failures"
    (out / "validation_report.json").write_text(json.dumps(report, indent=2, default=str))
    return report


def plain_sql(body: str, database: str, schema: str) -> str:
    """Resolve dbt refs to deployed relations (used for probes outside dbt)."""
    body = body.replace("{% raw %}", "").replace("{% endraw %}", "")
    return re.sub(r"\{\{\s*ref\('([^']+)'\)\s*\}\}", lambda m: f"{database}.{schema}.{m.group(1).upper()}", body)


def probe_sql(proj, database: str, schema: str) -> str:
    """One query returning the row count of every CTE in the workflow."""
    conv = proj.conv
    ctes = ",\n".join(f"{c} AS (\n    {plain_sql(s, database, schema)}\n)" for c, s in conv.ctes.items())
    counts = "\nUNION ALL ".join(f"SELECT {lit(c)} AS cte, COUNT(*) AS n FROM {c}" for c in conv.ctes)
    return f"WITH\n{ctes}\n{counts}"


def join_match_sql(proj, database: str, schema: str) -> str:
    """Rows of each join's left/right input that have a match (for the L + matched = input invariant)."""
    conv, wf = proj.conv, proj.wf
    parts = []
    for t in wf.tools.values():
        if t.type != "Join":
            continue
        try:
            (L,) = conv.inputs(t, "Left")
            (R,) = conv.inputs(t, "Right")
        except Exception:  # noqa: BLE001
            continue
        lk = [f.get("field") for f in t.config.findall("JoinInfo[@connection='Left']/Field")]
        rk = [f.get("field") for f in t.config.findall("JoinInfo[@connection='Right']/Field")]
        on = " AND ".join(f"EQUAL_NULL(l.{qid(L.find(a).name)}, r.{qid(R.find(b).name)})" for a, b in zip(lk, rk)
                          if L.find(a) and R.find(b))
        if not on:
            continue
        parts.append(f"SELECT {lit(t.id)} AS tool, "
                     f"(SELECT COUNT(*) FROM {L.cte} l WHERE EXISTS (SELECT 1 FROM {R.cte} r WHERE {on})) AS left_matched, "
                     f"(SELECT COUNT(*) FROM {R.cte} r WHERE EXISTS (SELECT 1 FROM {L.cte} l WHERE {on})) AS right_matched")
    if not parts:
        return ""
    ctes = ",\n".join(f"{c} AS (\n    {plain_sql(s, database, schema)}\n)" for c, s in conv.ctes.items())
    return f"WITH\n{ctes}\n" + "\nUNION ALL ".join(parts)


def invariants(proj, counts: dict[str, int], join_matches: dict[str, tuple[int, int]]) -> list[dict]:
    conv, wf = proj.conv, proj.wf
    out = []

    def n(tid, anchor):
        r = conv.out.get((tid, anchor))
        return counts.get(r.cte) if r else None

    for t in wf.tools.values():
        try:
            ins = conv.inputs(t)
        except Exception:  # noqa: BLE001
            continue
        inn = [counts.get(r.cte) for r in ins]
        chk = None
        if t.type == "Filter" and (t.id, "True") in conv.out and (t.id, "False") in conv.out:
            chk = ("True + False = input", n(t.id, "True") + n(t.id, "False"), inn[0])
        elif t.type == "Unique" and (t.id, "Unique") in conv.out and (t.id, "Duplicates") in conv.out:
            chk = ("Unique + Duplicates = input", n(t.id, "Unique") + n(t.id, "Duplicates"), inn[0])
        elif t.type == "Union":
            chk = ("output = sum(inputs)", n(t.id, "Output"), sum(x or 0 for x in inn))
        elif t.type in ("Select", "Formula", "Sort", "DynamicRename", "Macro:Cleanse", "RecordID"):
            chk = ("rows preserved", n(t.id, "Output"), inn[0])
        elif t.type == "Transpose":
            r = conv.out[(t.id, "Output")]
            k = len(re.findall(r"UNION ALL", conv.ctes[r.cte])) + 1
            chk = (f"output = input x {k} data fields", n(t.id, "Output"), (inn[0] or 0) * k)
        elif t.type == "Join" and t.id in join_matches:
            lm, rm = join_matches[t.id]
            L, R = conv.inputs(t, "Left")[0], conv.inputs(t, "Right")[0]
            if (t.id, "Left") in conv.out:
                out.append(_c(t, "Left unmatched + matched = left input", n(t.id, "Left") + lm, counts.get(L.cte)))
            if (t.id, "Right") in conv.out:
                out.append(_c(t, "Right unmatched + matched = right input", n(t.id, "Right") + rm, counts.get(R.cte)))
            continue
        if chk:
            out.append(_c(t, *chk))
    return out


def _c(t, rule, got, expected):
    return {"tool": t.id, "type": t.type, "rule": rule, "got": got, "expected": expected, "pass": got == expected}


# ------------------------------------------------------------------ semantics conformance
# (alteryx expression, field values, expected result) — expected values are Alteryx's documented behaviour
CONFORMANCE = [
    ('Round([x], 2)', {"x": ("num", "55.34")}, 56, "Round rounds to nearest multiple"),
    ('Round([x], .01)', {"x": ("num", "4.425")}, 4.43, "Round to 0.01"),
    ('Round([x], 0.1)', {"x": ("num", "4.42")}, 4.4, "Round to 0.1"),
    ('[s] + Null()', {"s": ("str", "'ABC'")}, "ABC", "String + Null = String"),
    ('[n] + Null()', {"n": ("num", "1")}, None, "Number + Null = Null"),
    ('[a] = [b]', {"a": ("str", "'abc'"), "b": ("str", "'ABC'")}, True, "string = is case-insensitive"),
    ('[a] = [b]', {"a": ("str", "NULL"), "b": ("str", "NULL")}, True, "Null = Null is True"),
    ('[a] != [b]', {"a": ("str", "NULL"), "b": ("str", "'x'")}, True, "Null != value is True"),
    ('[a] < [b]', {"a": ("num", "NULL"), "b": ("num", "1")}, False, "Null < 1 is False"),
    ('[a] <= [b]', {"a": ("num", "NULL"), "b": ("num", "NULL")}, True, "Null <= Null is True"),
    ('Contains([s], "ELL")', {"s": ("str", "'Hello'")}, True, "Contains default case-insensitive"),
    ('Contains([s], "ELL", 0)', {"s": ("str", "'Hello'")}, False, "Contains case-sensitive flag"),
    ('Contains([s], "x")', {"s": ("str", "NULL")}, False, "Contains on Null is False"),
    ('!Contains([s], "x")', {"s": ("str", "NULL")}, True, "NOT Contains on Null is True"),
    ('PadRight([s], 1, "0")', {"s": ("str", "'abc'")}, "abc", "PadRight never truncates"),
    ('PadLeft([s], 5, "0")', {"s": ("str", "'12'")}, "00012", "PadLeft pads"),
    ('Substring([s], 1, 2)', {"s": ("str", "'Hello'")}, "el", "Substring is 0-based"),
    ('FindString([s], "l")', {"s": ("str", "'Hello'")}, 2, "FindString is 0-based"),
    ('FindString([s], "z")', {"s": ("str", "'Hello'")}, -1, "FindString not found = -1"),
    ('IIF(IsNull([s]), "n", "v")', {"s": ("str", "NULL")}, "n", "IIF/IsNull"),
    ('IF [n] > 1 THEN "a" ELSEIF [n] = 1 THEN "b" ELSE "c" ENDIF', {"n": ("num", "1")}, "b", "IF/ELSEIF"),
    ('[a] / [b]', {"a": ("num", "1"), "b": ("num", "0")}, None, "divide by zero = Null"),
    ('Right([s], 2) + "/" + Left([s], 4)', {"s": ("str", "'2025_07'")}, "07/2025", "string concat"),
    ('substring(DateTimeParse(Left([p],3), "%b"),5,2)', {"p": ("str", "'Jul-2025'")}, "07", "DateTimeParse month name"),
    ('Replace([s], "Units", "Price")', {"s": ("str", "'Sales_Rev.Units'")}, "Sales_Rev.Price", "Replace"),
    ('Length([s]) > 2 or IsNull([s])', {"s": ("str", "NULL")}, True, "OR with IsNull"),
]


def conformance_sql() -> tuple[str, list]:
    rows = []
    for i, (ex, env, exp, desc) in enumerate(CONFORMANCE):
        sql, _, _ = to_sql(ex, lambda name, env=env: (f"v.{qid(name)}", env[name.lower()][0]))
        cols = ", ".join(f"{('NULL::FLOAT' if v == 'NULL' and k == 'num' else 'NULL::VARCHAR' if v == 'NULL' else v)} AS {qid(n)}"
                         for n, (k, v) in env.items())
        rows.append(f"SELECT {i} AS id, TO_VARCHAR(({sql})) AS got FROM (SELECT {cols}) v")
    return "\nUNION ALL ".join(rows) + "\nORDER BY id", CONFORMANCE


def conformance_check(results: dict[int, str | None]) -> list[dict]:
    out = []
    for i, (ex, _, exp, desc) in enumerate(CONFORMANCE):
        got = results.get(i)
        if exp is None:
            ok = got is None
        elif isinstance(exp, bool):
            ok = (got or "").lower() == str(exp).lower()
        elif isinstance(exp, (int, float)):
            try:
                ok = got is not None and abs(float(got) - exp) < 1e-9
            except ValueError:
                ok = False
        else:
            ok = got == exp
        out.append({"id": i, "expression": ex, "behaviour": desc, "expected": exp, "got": got, "pass": ok})
    return out


# ------------------------------------------------------------------ customer-run parity
def parity_sql(proj, model: dict, alteryx_table: str, target_db: str, target_schema: str) -> str:
    """Compare an Alteryx output (loaded into `alteryx_table`) with the converted model.

    Values are normalised so formatting noise does not count as a difference: numbers rounded to 6 dp,
    strings trimmed, empty string = NULL (Excel drops the distinction).
    """
    ours = f"{target_db}.{target_schema}.{model['model'].upper()}"
    norm = []
    for c, ty in zip(model["columns"], model["types"]):
        q = qid(c)
        if ty in ("Double", "Float", "Int16", "Int32", "Int64", "FixedDecimal", "Byte"):
            norm.append(f"ROUND(TRY_TO_DOUBLE(TO_VARCHAR({q})), 6) AS {q}")
        else:
            norm.append(f"NULLIF(TRIM(TO_VARCHAR({q})), '') AS {q}")
    sel = ", ".join(norm)
    return f"""-- Parity: Alteryx output vs Snowflake model {model['model']}
-- 1. Run the Alteryx workflow on the same input files; load its output into {alteryx_table}
--    (same column names; e.g. Snowsight > Load data, or COPY INTO from a stage).
-- 2. Point the dbt sources at the SAME input files (load them to the RAW schema) so both sides see identical inputs.
-- 3. Run this script. Every check should return 0 rows / equal counts.
SET a2s_model = '{ours}';
SET a2s_alteryx = '{alteryx_table}';

-- (a) row counts
SELECT 'alteryx' AS side, COUNT(*) AS n FROM IDENTIFIER($a2s_alteryx)
UNION ALL SELECT 'snowflake', COUNT(*) FROM IDENTIFIER($a2s_model);

-- (b) rows present on one side only. Duplicates are numbered so 2 copies vs 1 copy is still a difference.
WITH a AS (SELECT {sel} FROM IDENTIFIER($a2s_alteryx)),
     s AS (SELECT {sel} FROM IDENTIFIER($a2s_model)),
     a2 AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY HASH(*) ORDER BY 1) AS "_dup" FROM a),
     s2 AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY HASH(*) ORDER BY 1) AS "_dup" FROM s)
SELECT 'only_in_alteryx' AS side, * FROM (SELECT * FROM a2 MINUS SELECT * FROM s2)
UNION ALL
SELECT 'only_in_snowflake', * FROM (SELECT * FROM s2 MINUS SELECT * FROM a2)
ORDER BY 1
LIMIT 200;

-- (c) per-column fingerprint (pinpoints which column differs)
{column_fingerprint_sql(model, '$a2s_alteryx', '$a2s_model')}
"""


def column_fingerprint_sql(model: dict, a_var: str, s_var: str) -> str:
    parts = []
    for c, ty in zip(model["columns"], model["types"]):
        q = qid(c)
        x = (f"ROUND(TRY_TO_DOUBLE(TO_VARCHAR({q})), 6)" if ty in ("Double", "Float", "Int16", "Int32", "Int64", "FixedDecimal")
             else f"NULLIF(TRIM(TO_VARCHAR({q})), '')")
        parts.append(f"SELECT {lit(c)} AS column_name,\n"
                     f"       (SELECT HASH_AGG({x}) FROM IDENTIFIER({a_var})) AS alteryx_hash,\n"
                     f"       (SELECT HASH_AGG({x}) FROM IDENTIFIER({s_var})) AS snowflake_hash")
    return "SELECT column_name, alteryx_hash = snowflake_hash AS same FROM (\n" + "\nUNION ALL\n".join(parts) + "\n) ORDER BY same, column_name;"
