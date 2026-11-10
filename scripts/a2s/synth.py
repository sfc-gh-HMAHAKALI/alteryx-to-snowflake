"""Synthetic source data that exercises the workflow logic (joins match, filters fire).

Value domains are harvested from the workflow itself, never from customer data:
  * literals compared against a field in Filter/Formula expressions   -> candidate values for that field
  * Text Input (mapping table) values                                 -> pools for the fields they join to
  * join keys                                                         -> fields joined together share one pool
  * a table that joins to itself through two key fields (BOM)         -> parent/child values form an acyclic tree
"""
from __future__ import annotations

import csv
import json
import random
import re
from collections import defaultdict
from pathlib import Path

from .expr import Unsupported, parse
from .mappers import _walk, textinput_rows

PERIOD_RE = re.compile(r"^(\d{2}/\d{4}|[A-Z][a-z]{2}-\d{4}|\d{4}_\d{2}(_\d{2})?)$")


class UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def harvest(wf):
    lits = defaultdict(set)
    uf = UF()
    join_fields: set[str] = set()
    for t in wf.tools.values():
        exprs = []
        if t.type == "Formula":
            exprs = [f.get("expression") or "" for f in t.config.iter("FormulaField")]
        elif t.type == "Filter":
            if t.config.findtext("Mode") == "Custom":
                exprs = [t.config.findtext("Expression") or ""]
            else:
                s = t.config.find("Simple")
                op = s.findtext("Operand") or s.findtext("Operands/Operand")
                if op and s.findtext("Operator") in ("=", "!=", "Contains", "NotContains"):
                    lits[s.findtext("Field").lower()].add(op)
        elif t.type == "Join":
            lk = [f.get("field") for f in t.config.findall("JoinInfo[@connection='Left']/Field")]
            rk = [f.get("field") for f in t.config.findall("JoinInfo[@connection='Right']/Field")]
            for a, b in zip(lk, rk):
                uf.union(a.lower(), b.lower())
                join_fields |= {a.lower(), b.lower()}
        for ex in exprs:
            try:
                ast = parse(ex)
            except Unsupported:
                continue
            for n in _walk(ast):
                if n[0] == "cmp" and n[2][0] == "field" and n[3][0] in ("str", "num"):
                    lits[n[2][1].lower()].add(n[3][1])
                if n[0] == "call" and n[1] in ("contains", "startswith", "endswith") and len(n[2]) >= 2 \
                        and n[2][0][0] == "field" and n[2][1][0] == "str":
                    lits[n[2][0][1].lower()].add(("contains", n[2][1][1]))
    pools = defaultdict(list)
    for t in wf.tools.values():
        if t.type == "TextInput":
            names, rows = textinput_rows(t)
            for i, n in enumerate(names):
                pools[uf.find(n.lower())] += [r[i] for r in rows if r[i]]
    return lits, uf, pools, join_fields


def _num(rng, name):
    n = name.lower()
    if any(k in n for k in ("qty", "quantity", "units")):
        return rng.choice([0, 1, 1, 2, 3, 5, 10, 25])
    if "rate" in n or "conversion" in n:
        return round(rng.uniform(1.02, 1.18), 6)
    if re.search(r"(^|_)(id|key|numbr|num|number|seq|sequence)($|_)", n):
        return rng.randint(1, 99999)
    if any(k in n for k in ("cost", "price", "revenue", "amount", "value", "euro", "ptd", "ytd", "sum")) or PERIOD_RE.match(name):
        return round(rng.uniform(-50, 5000), 2) if rng.random() > 0.05 else 0
    return round(rng.uniform(0, 1000), 2)


def _date(rng):
    return f"{rng.choice([2024, 2025])}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"


def synth_rows(cols, n, wf_lits, uf, pools, rng, key_space, self_pairs, unique_keys=False, key_base=None):
    """cols: list of (name, kind). Returns list of rows (strings / numbers / None).

    self_pairs: {child_field: ('child', parent_field), parent_field: ('parent', None)} for a table that joins to itself
    (bill of materials). Parents are drawn from the first 20% of the key space (assemblies); a child is usually a
    leaf part and occasionally a later assembly, so the hierarchy is acyclic and grows ~1.5x per level.
    unique_keys: master tables get one row per key (keys assigned in sequence).
    """
    rows = []
    key_base = key_base or {}
    for i in range(n):
        row = {}
        for name, kind in cols:
            nl = name.lower()
            g = uf.find(nl)
            lit = list(wf_lits.get(nl, ()))
            v = None
            if g in key_space:
                space = key_space[g]
                if nl in self_pairs:
                    role, _ = self_pairs[nl]
                    v = space[rng.randrange(max(1, len(space) // 5))] if role == "parent" else None
                elif unique_keys:
                    v = space[i % len(space)]
                elif key_base.get(g) and rng.random() < 0.6:
                    v = rng.choice(key_base[g])  # values the workflow itself maps / filters on
                else:
                    v = rng.choice(space)
            elif lit and rng.random() < 0.7:
                c = rng.choice(lit)
                if isinstance(c, tuple):
                    v = f"{rng.choice(['', 'X ', 'Std '])}{c[1]}{rng.choice(['', ' Item', ' 01'])}"
                else:
                    v = c
            elif kind == "num" or PERIOD_RE.match(name):
                v = _num(rng, name)
            elif kind == "date" or re.search(r"(_dt|date|tsp)$", nl) or nl.endswith(" date"):
                v = _date(rng)
            elif kind == "bool":
                v = rng.choice([True, False])
            elif "email" in nl:
                v = f"user{rng.randint(1, 400)}@example.com"
            elif nl in ("currency",):
                v = rng.choice(["USD", "EUR", "USD"])
            elif "flag" in nl:
                v = rng.choice(["Y", "N"])
            elif "country" in nl:
                v = rng.choice(["United States of America", "France", "Germany", "Canada", "US", "FR", None])
            elif any(k in nl for k in ("name", "nm", "customer", "salesperson", "supervisor", "vendor")):
                v = f"{name.split()[0].split('_')[0].title()} {rng.randint(1, 250):03d}"
            else:
                v = f"{re.sub(r'[^A-Z]', '', name.upper())[:4] or 'V'}-{rng.randint(1, 60):03d}"
            if rng.random() < 0.02 and g not in key_space:
                v = None
            row[name] = v
        for child, (role, parent) in self_pairs.items():
            if role != "child":
                continue
            cname = next((c for c, _ in cols if c.lower() == child), None)
            pname = next((c for c, _ in cols if c.lower() == parent), None)
            if cname and pname and row.get(pname) is not None:
                space = key_space[uf.find(child)]
                a = max(2, len(space) // 5)
                pi = space.index(row[pname]) if row[pname] in space else 0
                if rng.random() < 0.03 and pi < a - 1:
                    row[cname] = space[rng.randrange(pi + 1, a)]   # sub-assembly
                else:
                    row[cname] = space[rng.randrange(a, len(space))]  # leaf part
        rows.append([row[c] for c, _ in cols])
    return rows


def _coerce(v, sf_type: str, rng):
    """Make a generated value loadable into its declared Snowflake column type."""
    if v is None:
        return None
    t = sf_type.upper()
    if t.startswith(("FLOAT", "NUMBER", "REAL")):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v) if t.endswith(",0)") else v
        s = str(v)
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            return int(float(s)) if t.endswith(",0)") else s
        return rng.randint(1, 99999)
    if t.startswith("TIMESTAMP") or t == "DATE":
        return v if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", str(v)) else _date(rng)
    if t == "BOOLEAN":
        return rng.choice(["true", "false"])
    m = re.match(r"VARCHAR\((\d+)\)", t)
    if m:
        return str(v)[: int(m.group(1))]
    return v


def synth_project(proj, out: Path, default_rows: int, row_counts: dict | None = None, seed: int = 7) -> dict:
    rng = random.Random(seed)
    wf = proj.wf
    lits, uf, pools, join_fields = harvest(wf)
    row_counts = row_counts or {}
    inputs = [e for e in proj.inputs.values() if e["source"] in ("raw", "mapped")]
    # key spaces for every join group that touches an input field
    groups = defaultdict(set)
    for e in inputs:
        for name, _ in e["fields"]:
            groups[uf.find(name.lower())].add(name.lower())
    key_space, key_base = {}, {}
    joined = {uf.find(x) for x in join_fields}
    is_master = lambda tbl: bool(re.search(r"MSTR|MASTER|COST", tbl))  # noqa: E731
    biggest = max(row_counts.values() or [default_rows])
    native_types = {}
    for e in inputs:
        if e["source"] == "mapped":
            for c, t in proj.smap[f"{wf.name}#{e['tool']}"]["columns"].items():
                native_types[c.lower()] = t
    numeric_t = lambda t: t.startswith(("FLOAT", "NUMBER", "REAL"))  # noqa: E731
    for g in joined:
        if g not in groups:
            continue
        numeric = any(numeric_t(native_types.get(f, "VARCHAR")) for f in groups[g])
        base = list(dict.fromkeys(pools.get(g, [])))
        for f in groups[g]:
            base += [x for x in lits.get(f, ()) if isinstance(x, str)]
        if numeric:
            base = [b for b in base if re.fullmatch(r"-?\d+(\.\d+)?", b)]
        # a master table needs one key per row, otherwise keys wrap and joins fan out
        master_rows = [row_counts.get(e["table"], default_rows) for e in inputs if is_master(e["table"])
                       and any(uf.find(n.lower()) == g for n, _ in e["fields"])]
        n = max([len(base) + 20, min(20000, biggest // 10)] + master_rows)
        prefix = re.sub(r"[^A-Z]", "", g.upper())[:3] or "K"
        gen = [str(100000 + i) for i in range(n)] if numeric else [f"{prefix}-{i:05d}" for i in range(n)]
        key_space[g] = list(dict.fromkeys(base + gen))
        key_base[g] = list(dict.fromkeys(base))
    out_dir = (out / proj.slug).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    loads, manifest = [], []
    for e in inputs:
        fields = e["fields"]
        fname_field = None
        if e["source"] == "raw":
            raw_cols = next(r["columns"] for r in proj.raw_tables if r["tool"] == e["tool"])
            cols = [(c, "str") for c in raw_cols]
            ddl_cols = [(c, "VARCHAR") for c in raw_cols]
            fq = f"{proj.raw_schema}.{e['table']}"
        else:
            native = proj.smap[f"{wf.name}#{e['tool']}"]["columns"]
            kinds = {"FLOAT": "num", "BOOLEAN": "bool", "TIMESTAMP_NTZ": "date", "DATE": "date"}
            cols = [(c, "num" if t.startswith("NUMBER") else kinds.get(t, "str")) for c, t in native.items()]
            ddl_cols = list(native.items())
            fq = f"{e['schema']}.{e['table']}"
        # Alteryx-declared numeric fields read from files must look numeric
        akind = {n.lower(): ("num" if t in ("Double", "Int16", "Int32", "Int64", "FixedDecimal", "Float") else "str") for n, t in fields}
        cols = [(c, "num" if akind.get(c.lower()) == "num" and k == "str" else k) for c, k in cols]
        names = {c.lower() for c, _ in cols}
        self_pairs = {}
        for g, members in groups.items():
            m = [x for x in members if x in names]
            if len(m) >= 2 and g in key_space:
                # e.g. PRODUCT_CODE (parent) / COMPONENT_PRODUCT_CODE (child) in the same table
                parent = next((x for x in m if "component" not in x), m[0])
                for c in m:
                    if c != parent:
                        self_pairs[c] = ("child", parent)
                self_pairs[parent] = ("parent", None)
        n = row_counts.get(e["table"], default_rows)
        uniq = bool(re.search(r"MSTR|MASTER|COST", e["table"])) and not self_pairs
        rows = synth_rows(cols, n, lits, uf, pools, rng, key_space, self_pairs, uniq, key_base)
        # Self-joined hierarchy (BOM): give each workflow-named root a 10-level chain with 2 leaves per level so
        # every level of a fixed-depth join explosion has rows (random data stays shallow).
        parent = next((c for c, (r, _) in self_pairs.items() if r == "parent"), None)
        if parent:
            child = next(c for c, (r, p) in self_pairs.items() if r == "child")
            ci = [c.lower() for c, _ in cols]
            pi_, ch_ = ci.index(parent), ci.index(child)
            g = uf.find(parent)
            roots = [k for k in key_base.get(g, []) if not re.fullmatch(r"\d+", k)][:5]
            # template rows that best satisfy the workflow's equality filters (e.g. PRODUCT_ORG_CODE = "002").
            # Literals are harvested per field name across the workflow, so score rather than require all.
            lit_cols = [(j, {x for x in lits.get(c, ()) if isinstance(x, str)}) for j, c in enumerate(ci)
                        if j not in (pi_, ch_) and lits.get(c)]
            scored = [(sum(r[j] in vals for j, vals in lit_cols), r) for r in rows]
            best = max((s for s, _ in scored), default=0)
            templates = [r for s, r in scored if s == best] or rows
            chain_rows = []
            for r_i, root in enumerate(roots):
                node = root
                for lvl in range(10):
                    nxt = f"{root}-L{lvl + 1}"
                    for kid in (nxt, f"{root}-L{lvl + 1}-P1", f"{root}-L{lvl + 1}-P2"):
                        row = list(templates[rng.randrange(len(templates))])
                        row[pi_], row[ch_] = node, kid
                        chain_rows.append(row)
                    node = nxt
            if chain_rows:
                rows = rows[: max(0, len(rows) - len(chain_rows))] + chain_rows
                self_pairs["_chains"] = ("roots", ",".join(roots))
        rows = [[_coerce(v, t, rng) for v, (_, t) in zip(r, ddl_cols)] for r in rows]
        path = out_dir / f"{e['table'].lower()}.csv"
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([c for c, _ in cols])
            for r in rows:
                w.writerow(["" if v is None else v for v in r])
        manifest.append({"table": fq, "rows": n, "file": str(path), "self_join_hierarchy": sorted(self_pairs)})
        ddl = ", ".join('"%s" %s' % (c.replace('"', '""'), t) for c, t in ddl_cols)
        loads.append(f"CREATE OR REPLACE TABLE {fq} ({ddl});\n"
                     f"PUT 'file://{path}' @{proj.raw_schema}.SYNTH_STAGE/{proj.slug}/ AUTO_COMPRESS=TRUE OVERWRITE=TRUE;\n"
                     f"COPY INTO {fq} FROM @{proj.raw_schema}.SYNTH_STAGE/{proj.slug}/{path.name}.gz\n"
                     f"  FILE_FORMAT = (TYPE = CSV PARSE_HEADER = TRUE FIELD_OPTIONALLY_ENCLOSED_BY = '\"' EMPTY_FIELD_AS_NULL = TRUE)\n"
                     f"  MATCH_BY_COLUMN_NAME = CASE_SENSITIVE;\n")
    (out_dir / "load.sql").write_text("\n".join(loads))
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return {"workflow": wf.name, "dir": str(out_dir), "tables": manifest}
