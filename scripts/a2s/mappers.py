"""Per-tool mappers: each Alteryx tool becomes one or more SQL CTEs with a tracked output schema."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .expr import Unsupported, eval_py, lit, parse, qid, to_sql
from .parse import Field, NUMERIC_ATYPES, STRING_ATYPES, Tool, Workflow


@dataclass
class Rel:
    cte: str
    fields: list[Field]
    order: list[tuple[str, str]] | None = None  # [(field, ASC|DESC)] from the nearest upstream Sort

    def __post_init__(self):
        # an ordering survives only while its columns do (a Select may drop or rename them)
        if self.order:
            names = {f.name.lower() for f in self.fields}
            self.order = [(c, d) for c, d in self.order if c.lower() in names] or None

    def find(self, name: str) -> Field | None:
        nl = name.lower()
        for f in self.fields:
            if f.name.lower() == nl:
                return f
        return None


@dataclass
class Issue:
    tool: str
    severity: str  # TODO | WARN | NOTE
    message: str


def kind_of(atype: str) -> str:
    return Field("x", atype).kind


def cast_expr(expr: str, from_kind: str, atype: str, size: str | None = None) -> str:
    """Convert a value to an Alteryx type the way Alteryx's Select/Formula would."""
    to = kind_of(atype)
    sf = Field("x", atype, size).sf_type
    if to == "num":
        if from_kind == "str":
            fn = "TRY_TO_DOUBLE" if atype in ("Double", "Float") else "TRY_TO_DECIMAL" if atype == "FixedDecimal" else "TRY_TO_NUMBER"
            if fn == "TRY_TO_DECIMAL":
                p, s = (size or "19.6").split(".") if size and "." in size else ("38", "6")
                return f"TRY_TO_DECIMAL({expr}, {p}, {s})"
            return f"{fn}({expr})"
        if from_kind == "bool":
            return f"IFF({expr}, 1, 0)"
        return expr if sf == "FLOAT" and from_kind == "num" else f"CAST({expr} AS {sf})"
    if to == "str":
        if from_kind == "num":
            out = f"TO_VARCHAR({expr})"
        elif from_kind == "date":
            out = f"TO_VARCHAR({expr}, 'YYYY-MM-DD HH24:MI:SS')"
        elif from_kind == "bool":
            out = f"IFF({expr}, 'True', 'False')"
        else:
            out = expr
        if size and size.isdigit() and int(size) < 100000 and atype in STRING_ATYPES:
            out = f"LEFT({out}, {size})"
        return out
    if to == "date":
        if from_kind == "date":
            return f"CAST({expr} AS {sf})"
        fn = "TRY_TO_DATE" if atype == "Date" else "TRY_TO_TIME" if atype == "Time" else "TRY_TO_TIMESTAMP"
        return f"{fn}(TO_VARCHAR({expr}))"
    if to == "bool":
        return expr if from_kind == "bool" else f"TRY_TO_BOOLEAN(TO_VARCHAR({expr}))"
    return expr


def sanitize(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    return ("t_" + s) if s[:1].isdigit() else s or "unnamed"


class Converter:
    def __init__(self, wf: Workflow, source_relations: dict[str, str], null_safe_joins: bool = True):
        self.wf = wf
        self.src = source_relations  # tool id -> dbt relation expression for staging/seed
        self.null_safe = null_safe_joins
        self.out: dict[tuple[str, str], Rel] = {}
        self.ctes: dict[str, str] = {}  # cte name -> sql (insertion order = topo order)
        self.cte_deps: dict[str, set[str]] = {}
        self.cte_tool: dict[str, str] = {}
        self.issues: list[Issue] = []
        self.tests: list[dict] = []
        self.schema_checks: list[dict] = []

    # ---------------------------------------------------------------- helpers
    def issue(self, t: Tool, sev: str, msg: str):
        self.issues.append(Issue(t.id, sev, f"{t.label}: {msg}"))

    def add_cte(self, t: Tool, suffix: str, sql: str, deps: list[Rel]) -> str:
        name = f"t{t.id}_{sanitize(t.type)}{('_' + suffix) if suffix else ''}"
        i = 2
        base = name
        while name in self.ctes:
            name = f"{base}_{i}"
            i += 1
        self.ctes[name] = sql
        self.cte_deps[name] = {d.cte for d in deps}
        self.cte_tool[name] = t.id
        return name

    def inputs(self, t: Tool, anchor: str | None = None) -> list[Rel]:
        edges = [e for e in self.wf.inputs_of(t.id) if anchor is None or e.dst_anchor == anchor]
        edges.sort(key=lambda e: int(e.name[1:]) if e.name and e.name[1:].isdigit() else 0)
        rels = []
        for e in edges:
            r = self.out.get((e.src, e.src_anchor))
            if r is None:
                raise Unsupported(f"upstream {e.src}:{e.src_anchor} unavailable")
            rels.append(r)
        return rels

    def resolver(self, rel: Rel, alias: str | None = None, extra: dict[str, Field] | None = None):
        def res(name):
            if extra and name.lower() in extra:
                f = extra[name.lower()]
            else:
                f = rel.find(name)
            if f is None:
                raise KeyError(name)
            return (f"{alias}.{qid(f.name)}" if alias else qid(f.name)), f.kind
        return res

    @staticmethod
    def cols(fields: list[Field], alias: str | None = None) -> str:
        p = f"{alias}." if alias else ""
        return ", ".join(p + qid(f.name) for f in fields) or "NULL AS \"_empty\""

    # ---------------------------------------------------------------- driver
    def run(self):
        qs = getattr(self.wf, "app_questions", [])
        if qs:
            self.issues.append(Issue("-", "TODO", f"analytic app has {len(qs)} user questions ({', '.join(qs[:5])}); "
                                                  "map each to a dbt var / procedure argument by hand"))
        for tid in self.wf.topo_order():
            t = self.wf.tools[tid]
            fn = getattr(self, "m_" + sanitize(t.type.replace("Macro:", "macro_")), None)
            try:
                if fn is None:
                    raise Unsupported(f"no mapper for tool type {t.type}")
                fn(t)
            except Unsupported as u:
                self.passthrough(t, str(u))
            self.check_schema(t)
        return self

    def passthrough(self, t: Tool, why: str):
        self.issue(t, "TODO", f"not translated ({why}); emitted pass-through placeholder")
        try:
            ins = self.inputs(t)
        except Unsupported:
            ins = []
        src = ins[0] if ins else None
        anchors = {e.src_anchor for e in self.wf.outputs_of(t.id)} or {"Output"}
        for a in sorted(anchors):
            meta = t.meta.get(a)
            if meta:
                # Shape matches Alteryx's cached schema so downstream SQL still compiles
                cols = ", ".join(f"NULL::{f.sf_type} AS {qid(f.name)}" for f in meta)
                body = f"SELECT {cols} FROM {src.cte}" if src else f"SELECT {cols} WHERE FALSE"
                fields = meta
            elif src:
                body, fields = f"SELECT * FROM {src.cte}", list(src.fields)
            else:
                body, fields = "SELECT NULL AS \"_todo\" WHERE FALSE", []
            sql = f"-- TODO(ALTERYX:{t.id}) {t.type} anchor {a}: {why}\n    {body}"
            self.out[(t.id, a)] = Rel(self.add_cte(t, sanitize(a), sql, ins), fields)

    def check_schema(self, t: Tool):
        for anchor, expected in t.meta.items():
            got = self.out.get((t.id, anchor))
            if got is None or t.type in ("Input", "TextInput"):
                continue
            exp_names = [f.name for f in expected]
            got_names = [f.name for f in got.fields]
            name_ok = [n.lower() for n in exp_names] == [n.lower() for n in got_names]
            type_diff = []
            if name_ok:
                for e, g in zip(expected, got.fields):
                    if e.kind != g.kind:
                        type_diff.append(f"{e.name}: alteryx {e.atype} vs ours {g.atype}")
            self.schema_checks.append({"tool": t.id, "type": t.type, "anchor": anchor, "names_match": name_ok,
                                       "type_mismatches": type_diff,
                                       "missing": [n for n in exp_names if n.lower() not in {x.lower() for x in got_names}],
                                       "extra": [n for n in got_names if n.lower() not in {x.lower() for x in exp_names}]})
            if not name_ok or type_diff:
                self.issue(t, "WARN", f"schema differs from Alteryx cached metadata on anchor {anchor}")

    # ---------------------------------------------------------------- sources
    def m_input(self, t: Tool):
        fields = t.meta.get("Output", [])
        sql = f"SELECT * FROM {self.src[t.id]}"
        self.out[(t.id, "Output")] = Rel(self.add_cte(t, "", sql, []), fields)

    def m_textinput(self, t: Tool):
        fields = textinput_fields(t)
        sql = f"SELECT * FROM {self.src[t.id]}"
        self.out[(t.id, "Output")] = Rel(self.add_cte(t, "", sql, []), fields)

    # ---------------------------------------------------------------- column tools
    def apply_select(self, items, entries, order_changed: bool, t: Tool):
        """items: list of (key, src_expr, Field default_out). Returns list of (expr, Field)."""
        explicit = {}
        for e in entries:
            if e.get("field") != "*Unknown":
                explicit.setdefault(e.get("field").lower(), e)
        unknown = next((e for e in entries if e.get("field") == "*Unknown"), None)
        unknown_sel = unknown is None or unknown.get("selected", "True") == "True"

        def render(item, e):
            key, expr, f = item
            if e is not None and e.get("selected", "True") == "False":
                return None
            name = (e.get("rename") if e is not None and e.get("rename") else None) or f.name
            atype, size = f.atype, f.size
            if e is not None and e.get("type") and (e.get("type") != f.atype or (e.get("size") and e.get("size") != f.size)):
                atype, size = e.get("type"), e.get("size")
                expr = cast_expr(expr, f.kind, atype, size)
            return expr, Field(name, atype, size, f.scale)

        out = []
        if order_changed:
            by_key = {k.lower(): (k, x, f) for k, x, f in items}
            for e in entries:
                if e.get("field") == "*Unknown":
                    if unknown_sel:
                        for it in items:
                            if it[0].lower() not in explicit:
                                r = render(it, None)
                                if r:
                                    out.append(r)
                    continue
                it = by_key.get(e.get("field").lower())
                if it:
                    r = render(it, e)
                    if r:
                        out.append(r)
        else:
            for it in items:
                e = explicit.get(it[0].lower())
                if e is None and not unknown_sel:
                    continue
                r = render(it, e)
                if r:
                    out.append(r)
        seen: dict[str, int] = {}
        deduped = []
        for x, f in out:
            k = f.name.lower()
            if k in seen:
                seen[k] += 1
                new = f"{f.name}{seen[k]}"
                self.issue(t, "WARN", f"duplicate output field {f.name!r} renamed to {new!r} (assumed Alteryx suffix rule; verify)")
                f = Field(new, f.atype, f.size, f.scale)
            else:
                seen[k] = 1
            deduped.append((x, f))
        return deduped

    def m_select(self, t: Tool):
        (src,) = self.inputs(t)
        items = [(f.name, qid(f.name), f) for f in src.fields]
        entries = t.config.findall("SelectFields/SelectField")
        oc = (t.config.find("OrderChanged") is not None and t.config.find("OrderChanged").get("value") == "True")
        sel = self.apply_select(items, entries, oc, t)
        body = ", ".join(x if x == qid(f.name) else f"{x} AS {qid(f.name)}" for x, f in sel)
        name = self.add_cte(t, "", f"SELECT {body or 'NULL AS x'} FROM {src.cte}", [src])
        self.out[(t.id, "Output")] = Rel(name, [f for _, f in sel], src.order)

    def m_formula(self, t: Tool):
        (src,) = self.inputs(t)
        rel = src
        pending: list[tuple[str, str, Field, bool]] = []
        cur_fields = list(src.fields)
        pend_names: set[str] = set()
        step = 0

        def flush():
            nonlocal rel, pending, pend_names, step
            if not pending:
                return
            repl = [f"{e} AS {qid(f.name)}" for _, e, f, r in pending if r]
            add = [f"{e} AS {qid(f.name)}" for _, e, f, r in pending if not r]
            star = "*" + (f" REPLACE ({', '.join(repl)})" if repl else "")
            sep = ",\n           "
            sql = f"SELECT {sep.join([star] + add)}\n    FROM {rel.cte}"
            step += 1
            rel = Rel(self.add_cte(t, "" if step == 1 else f"step{step}", sql, [rel]), list(cur_fields), src.order)
            pending, pend_names = [], set()

        for ff in t.config.iter("FormulaField"):
            fname, ex = ff.get("field"), ff.get("expression") or ""
            try:
                refs = {n[1].lower() for n in _walk(parse(ex)) if n[0] == "field"}
            except Unsupported as u:
                raise Unsupported(f"formula [{fname}]: {u}")
            if refs & pend_names:
                flush()
            cur = Rel(rel.cte, cur_fields)
            try:
                sql, kind, notes = to_sql(ex, self.resolver(cur))
            except Unsupported as u:
                raise Unsupported(f"formula [{fname}]: {u}")
            for nmsg in notes:
                self.issue(t, "NOTE", f"[{fname}] {nmsg}")
            existing = cur.find(fname)
            if existing is not None:
                target = existing
            else:
                target = Field(fname, ff.get("type") or "V_WString", ff.get("size"))
            expr = sql if kind == target.kind or kind == "null" else cast_expr(sql, kind, target.atype, target.size)
            if kind == "null":
                expr = f"NULL::{target.sf_type}"
            if existing is not None:
                fname = existing.name
                pending.append((fname, expr, existing, True))
            else:
                pending.append((fname, expr, target, False))
                cur_fields.append(target)
            pend_names.add(fname.lower())
        flush()
        self.out[(t.id, "Output")] = Rel(rel.cte, list(cur_fields), src.order)

    def m_filter(self, t: Tool):
        (src,) = self.inputs(t)
        mode = t.config.findtext("Mode")
        if mode == "Custom":
            ex = t.config.findtext("Expression") or ""
        else:
            s = t.config.find("Simple")
            fld, op = s.findtext("Field"), s.findtext("Operator")
            operand = s.findtext("Operands/Operand") or ""
            f = src.find(fld)
            if f is None:
                raise Unsupported(f"filter field [{fld}] not found")
            q = '"' + operand.replace('"', '""') + '"'
            val = operand if f.kind == "num" and re.fullmatch(r"-?\d+(\.\d+)?", operand.strip()) else q
            ex = {
                "=": f"[{fld}] = {val}", "!=": f"[{fld}] != {val}", "<": f"[{fld}] < {val}", ">": f"[{fld}] > {val}",
                "<=": f"[{fld}] <= {val}", ">=": f"[{fld}] >= {val}", "Contains": f"Contains([{fld}], {q})",
                "NotContains": f"!Contains([{fld}], {q})", "IsNull": f"IsNull([{fld}])", "IsNotNull": f"!IsNull([{fld}])",
                "IsEmpty": f"IsEmpty([{fld}])", "IsNotEmpty": f"!IsEmpty([{fld}])",
            }.get(op)
            if ex is None:
                raise Unsupported(f"simple filter operator {op}")
        cond, kind, notes = to_sql(ex, self.resolver(src))
        for nmsg in notes:
            self.issue(t, "NOTE", nmsg)
        if kind != "bool":
            cond = f"COALESCE({cond} <> 0, FALSE)" if kind == "num" else f"COALESCE({cond}, FALSE)"
        used = {e.src_anchor for e in self.wf.outputs_of(t.id)}
        for anchor, where in (("True", cond), ("False", f"NOT ({cond})")):
            if anchor in used or not used:
                name = self.add_cte(t, anchor.lower(), f"SELECT * FROM {src.cte}\n    WHERE {where}", [src])
                self.out[(t.id, anchor)] = Rel(name, list(src.fields), src.order)

    def m_join(self, t: Tool):
        if t.config.get("joinByRecordPos") == "True":
            raise Unsupported("join by record position")
        (L,) = self.inputs(t, "Left")
        (R,) = self.inputs(t, "Right")
        lk = [f.get("field") for f in t.config.findall("JoinInfo[@connection='Left']/Field")]
        rk = [f.get("field") for f in t.config.findall("JoinInfo[@connection='Right']/Field")]
        if len(lk) != len(rk) or not lk:
            raise Unsupported("join keys unbalanced")
        conds = []
        for a, b in zip(lk, rk):
            fa, fb = L.find(a), R.find(b)
            if fa is None or fb is None:
                raise Unsupported(f"join key {a}/{b} missing")
            x, y = f"l.{qid(fa.name)}", f"r.{qid(fb.name)}"
            if fa.kind != fb.kind:
                self.issue(t, "WARN", f"join key types differ ({fa.atype} vs {fb.atype}); compared as text")
                x, y = cast_expr(x, fa.kind, "V_WString"), cast_expr(y, fb.kind, "V_WString")
            conds.append(f"EQUAL_NULL({x}, {y})" if self.null_safe else f"{x} = {y}")
        on = " AND ".join(conds)
        used = {e.src_anchor for e in self.wf.outputs_of(t.id)}
        if "Join" in used or not used:
            sc = t.config.find("SelectConfiguration/Configuration")
            entries = sc.findall("SelectFields/SelectField") if sc is not None else []
            oc = sc is not None and sc.find("OrderChanged") is not None and sc.find("OrderChanged").get("value") == "True"
            # Right fields get a "Right_" prefix only when a same-named left field is still selected
            cfg = {e.get("field").lower(): e for e in entries}
            unk = cfg.get("*unknown")
            unk_sel = unk is None or unk.get("selected", "True") == "True"
            left_kept = set()
            for f in L.fields:
                e = cfg.get(("left_" + f.name).lower())
                if (e.get("selected", "True") == "True") if e is not None else unk_sel:
                    left_kept.add(((e.get("rename") if e is not None and e.get("rename") else f.name)).lower())
            items = [("Left_" + f.name, f"l.{qid(f.name)}", f) for f in L.fields]
            for f in R.fields:
                out_name = ("Right_" + f.name) if f.name.lower() in left_kept else f.name
                items.append(("Right_" + f.name, f"r.{qid(f.name)}", Field(out_name, f.atype, f.size, f.scale)))
            sel = self.apply_select(items, entries, oc, t)
            body = ",\n           ".join(f"{x} AS {qid(f.name)}" for x, f in sel)
            sql = f"SELECT {body}\n    FROM {L.cte} l\n    JOIN {R.cte} r ON {on}"
            self.out[(t.id, "Join")] = Rel(self.add_cte(t, "j", sql, [L, R]), [f for _, f in sel])
        if "Left" in used:
            sql = f"SELECT l.* FROM {L.cte} l\n    WHERE NOT EXISTS (SELECT 1 FROM {R.cte} r WHERE {on})"
            self.out[(t.id, "Left")] = Rel(self.add_cte(t, "l", sql, [L, R]), list(L.fields), L.order)
        if "Right" in used:
            sql = f"SELECT r.* FROM {R.cte} r\n    WHERE NOT EXISTS (SELECT 1 FROM {L.cte} l WHERE {on})"
            self.out[(t.id, "Right")] = Rel(self.add_cte(t, "r", sql, [L, R]), list(R.fields), R.order)

    def m_appendfields(self, t: Tool):
        (T,) = self.inputs(t, "Targets")
        (S,) = self.inputs(t, "Source")
        tn = {f.name.lower() for f in T.fields}
        sf = [Field(("Source_" + f.name) if f.name.lower() in tn else f.name, f.atype, f.size) for f in S.fields]
        body = ", ".join([f"t.{qid(f.name)}" for f in T.fields] +
                         [f"s.{qid(o.name)} AS {qid(n.name)}" for o, n in zip(S.fields, sf)])
        sql = f"SELECT {body} FROM {T.cte} t CROSS JOIN {S.cte} s"
        self.out[(t.id, "Output")] = Rel(self.add_cte(t, "", sql, [T, S]), T.fields + sf)

    def m_union(self, t: Tool):
        ins = self.inputs(t)
        mode = t.config.findtext("Mode") or "ByName"
        if mode == "ByName":
            sub = (t.config.findtext("ByName_OutputMode") or "All") == "Subset"
            names: list[Field] = []
            seen = {}
            for r in ins:
                for f in r.fields:
                    k = f.name.lower()
                    if k not in seen:
                        seen[k] = f
                        names.append(f)
            if sub:
                names = [f for f in names if all(r.find(f.name) for r in ins)]
            mapping = [[r.find(f.name) for f in names] for r in ins]
        elif mode in ("ByPos", "Manual", "ManualDelayed"):
            names = list(ins[0].fields)
            mapping = [[r.fields[i] if i < len(r.fields) else None for i in range(len(names))] for r in ins]
            if mode != "ByPos":
                self.issue(t, "NOTE", "manual field mapping treated as by-position (names from input #1)")
        else:
            raise Unsupported(f"union mode {mode}")
        meta = t.meta.get("Output")
        out_fields = []
        for i, f in enumerate(names):
            kinds = {m[i].kind for m in mapping if m[i] is not None}
            if meta and i < len(meta) and meta[i].name.lower() == f.name.lower():
                out_fields.append(Field(f.name, meta[i].atype, meta[i].size))
            elif len(kinds) <= 1:
                out_fields.append(f)
            else:
                out_fields.append(Field(f.name, "Double" if kinds <= {"num"} else "V_WString"))
        parts = []
        for r, m in zip(ins, mapping):
            cols = []
            for src_f, of in zip(m, out_fields):
                if src_f is None:
                    cols.append(f"NULL::{of.sf_type} AS {qid(of.name)}")
                else:
                    x = qid(src_f.name)
                    if src_f.kind != of.kind:
                        x = cast_expr(x, src_f.kind, of.atype, of.size)
                    cols.append(f"{x} AS {qid(of.name)}")
            parts.append(f"SELECT {', '.join(cols)} FROM {r.cte}")
        sql = "\n    UNION ALL\n    ".join(parts)
        self.out[(t.id, "Output")] = Rel(self.add_cte(t, "", sql, ins), out_fields)

    # ---------------------------------------------------------------- aggregation
    AGG = {"Sum": "SUM", "Min": "MIN", "Max": "MAX", "Avg": "AVG"}

    def m_summarize(self, t: Tool):
        (src,) = self.inputs(t)
        groups, sels, fields = [], [], []
        for s in t.config.findall("SummarizeFields/SummarizeField"):
            f = src.find(s.get("field"))
            act, out = s.get("action"), s.get("rename") or s.get("field")
            if f is None:
                raise Unsupported(f"summarize field {s.get('field')} missing")
            col = qid(f.name)
            if act == "GroupBy":
                groups.append(col)
                sels.append(f"{col} AS {qid(out)}")
                fields.append(Field(out, f.atype, f.size))
                continue
            if act in self.AGG:
                x = col if f.kind == "num" or act in ("Min", "Max") else f"TRY_TO_DOUBLE({col})"
                if f.kind != "num" and act not in ("Min", "Max"):
                    self.issue(t, "WARN", f"{act} over non-numeric field {f.name}")
                sels.append(f"{self.AGG[act]}({x}) AS {qid(out)}")
                fields.append(Field(out, "Double" if act in ("Sum", "Avg") else f.atype, f.size))
            elif act == "Count":
                sels.append(f"COUNT(*) AS {qid(out)}")
                fields.append(Field(out, "Int64"))
            elif act == "CountNonNull":
                sels.append(f"COUNT({col}) AS {qid(out)}")
                fields.append(Field(out, "Int64"))
            elif act == "CountNull":
                sels.append(f"COUNT_IF({col} IS NULL) AS {qid(out)}")
                fields.append(Field(out, "Int64"))
            elif act == "CountDistinct":
                sels.append(f"(COUNT(DISTINCT {col}) + IFF(COUNT_IF({col} IS NULL) > 0, 1, 0)) AS {qid(out)}")
                fields.append(Field(out, "Int64"))
            elif act == "CountDistinctNonNull":
                sels.append(f"COUNT(DISTINCT {col}) AS {qid(out)}")
                fields.append(Field(out, "Int64"))
            elif act in ("Concat", "Concatenate"):
                sep = s.get("separator", ",")
                sels.append(f"LISTAGG({col}, {lit(sep)}) AS {qid(out)}")
                fields.append(Field(out, "V_WString"))
                self.issue(t, "NOTE", f"Concatenate order is not guaranteed (Alteryx uses record order)")
            elif act in ("First", "Last"):
                if src.order:
                    # ARRAY_AGG drops NULLs; wrap in an object so a NULL first/last value is kept like Alteryx does
                    ob = ", ".join(f"{qid(c)} {d if act == 'First' else ('DESC' if d == 'ASC' else 'ASC')}" for c, d in src.order)
                    agg = f"ARRAY_AGG(OBJECT_CONSTRUCT_KEEP_NULL('v', {col})) WITHIN GROUP (ORDER BY {ob})[0]:v"
                    sels.append(f"{agg}::{f.sf_type} AS {qid(out)}")
                else:
                    sels.append(f"ANY_VALUE({col}) AS {qid(out)}")
                    self.issue(t, "WARN", f"{act} depends on record order; no upstream Sort, used ANY_VALUE")
                fields.append(Field(out, f.atype, f.size))
            else:
                raise Unsupported(f"summarize action {act}")
        sql = f"SELECT {', '.join(sels)}\n    FROM {src.cte}" + (f"\n    GROUP BY {', '.join(groups)}" if groups else "")
        # Alteryx sorts Summarize output by the group-by fields
        self.out[(t.id, "Output")] = Rel(self.add_cte(t, "", sql, [src]), fields,
                                         [(f.name, "ASC") for f, g in zip(fields, sels) if g.split(" AS ")[0] in groups])

    def m_unique(self, t: Tool):
        (src,) = self.inputs(t)
        keys = [src.find(f.get("field")) for f in t.config.findall("UniqueFields/Field")]
        if None in keys:
            raise Unsupported("unique key missing")
        part = ", ".join(qid(k.name) for k in keys)
        if src.order:
            ob = ", ".join(f"{qid(c)} {d} NULLS {'FIRST' if d == 'ASC' else 'LAST'}" for c, d in src.order)
        else:
            ob = "NULL"
            self.issue(t, "WARN", "kept record depends on input order (no upstream Sort); non-key columns may differ from Alteryx")
        numbered = self.add_cte(t, "rn", f"SELECT *, ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY {ob}) AS \"__a2s_rn\"\n    FROM {src.cte}", [src])
        nrel = Rel(numbered, src.fields)
        used = {e.src_anchor for e in self.wf.outputs_of(t.id)}
        for anchor, cond in (("Unique", "= 1"), ("Duplicates", "> 1")):
            if anchor in used or not used:
                name = self.add_cte(t, anchor[0].lower(), f"SELECT * EXCLUDE (\"__a2s_rn\") FROM {numbered} WHERE \"__a2s_rn\" {cond}", [nrel])
                self.out[(t.id, anchor)] = Rel(name, list(src.fields), src.order)

    def m_sort(self, t: Tool):
        (src,) = self.inputs(t)
        order = []
        for f in t.config.findall("SortInfo/Field"):
            fld = src.find(f.get("field"))
            if fld is None:
                raise Unsupported(f"sort field {f.get('field')} missing")
            order.append((fld.name, "DESC" if f.get("order") == "Descending" else "ASC"))
        ob = ", ".join(f"{qid(c)} {d}" for c, d in order)
        name = self.add_cte(t, "", f"SELECT * FROM {src.cte}  -- ORDER BY {ob} (applied by consumers: Unique, final select)", [src])
        self.out[(t.id, "Output")] = Rel(name, list(src.fields), order)

    def m_recordid(self, t: Tool):
        (src,) = self.inputs(t)
        fname = t.config.findtext("FieldName") or "RecordID"
        start = t.config.findtext("StartValue") or "1"
        ob = ", ".join(f"{qid(c)} {d}" for c, d in src.order) if src.order else "NULL"
        if not src.order:
            self.issue(t, "WARN", "RecordID depends on record order; no upstream Sort")
        pos = t.config.findtext("Position") or "0"
        x = f"(ROW_NUMBER() OVER (ORDER BY {ob}) + {int(start) - 1}) AS {qid(fname)}"
        sql = f"SELECT {x}, * FROM {src.cte}" if pos == "0" else f"SELECT *, {x} FROM {src.cte}"
        nf = Field(fname, t.config.findtext("FieldType") or "Int32")
        self.out[(t.id, "Output")] = Rel(self.add_cte(t, "", sql, [src]),
                                         ([nf] + src.fields) if pos == "0" else (src.fields + [nf]), src.order)

    # ---------------------------------------------------------------- reshape
    @staticmethod
    def header_name(sql_val: str) -> str:
        # Cross Tab turns header values into field names, replacing invalid characters with "_"
        return f"REGEXP_REPLACE(TO_VARCHAR({sql_val}), '[^A-Za-z0-9_]', '_')"

    def m_crosstab(self, t: Tool):
        (src,) = self.inputs(t)
        groups = [src.find(f.get("field")) for f in t.config.findall("GroupFields/Field")]
        hdr = src.find(t.config.find("HeaderField").get("field"))
        dat = src.find(t.config.find("DataField").get("field"))
        methods = [m.get("method") for m in t.config.findall("Methods/Method")]
        if None in groups or hdr is None or dat is None:
            raise Unsupported("crosstab field missing")
        meta = t.meta.get("Output")
        if not meta:
            raise Unsupported("CrossTab output columns are data-dependent and no cached metadata exists; "
                              "use PIVOT ... IN (ANY) or list header values")
        gnames = {g.name.lower() for g in groups}
        value_fields = [f for f in meta if f.name.lower() not in gnames]
        fn = {"Sum": "SUM", "Count": "COUNT", "Max": "MAX", "Min": "MIN", "Avg": "AVG", "CountDistinct": "COUNT_DISTINCT"}
        sels = [qid(g.name) for g in groups]
        hx = self.header_name(qid(hdr.name))
        for vf in value_fields:
            meth, val = methods[0], vf.name
            if len(methods) > 1 and "_" in vf.name:
                meth, val = vf.name.split("_", 1)
            if meth not in fn and meth not in ("First", "Last", "Concat"):
                raise Unsupported(f"crosstab method {meth}")
            x = qid(dat.name) if dat.kind == "num" or meth not in ("Sum", "Avg") else f"TRY_TO_DOUBLE({qid(dat.name)})"
            cond = f"{hx} = {lit(val)}"
            if meth == "CountDistinct":
                sels.append(f"COUNT(DISTINCT IFF({cond}, {x}, NULL)) AS {qid(vf.name)}")
            elif meth in fn:
                sels.append(f"{fn[meth]}(IFF({cond}, {x}, NULL)) AS {qid(vf.name)}")
            elif meth == "Concat":
                sels.append(f"LISTAGG(IFF({cond}, {qid(dat.name)}, NULL), ',') AS {qid(vf.name)}")
            else:
                sels.append(f"ANY_VALUE(IFF({cond}, {x}, NULL)) AS {qid(vf.name)}")
                self.issue(t, "WARN", f"CrossTab {meth} depends on record order")
        self.issue(t, "NOTE", f"pivot columns fixed from Alteryx metadata ({len(value_fields)} values); "
                              f"new header values in future data will not appear without regeneration")
        sql = f"SELECT {', '.join(sels)}\n    FROM {src.cte}\n    GROUP BY {', '.join(qid(g.name) for g in groups)}"
        fields = [Field(g.name, g.atype, g.size) for g in groups] + [Field(f.name, f.atype, f.size) for f in value_fields]
        self.out[(t.id, "Output")] = Rel(self.add_cte(t, "", sql, [src]), fields)

    def m_transpose(self, t: Tool):
        (src,) = self.inputs(t)
        keys = [src.find(f.get("field")) for f in t.config.findall("KeyFields/Field")]
        keys = [k for k in keys if k is not None]
        knames = {k.name.lower() for k in keys}
        entries = t.config.findall("DataFields/Field")
        explicit = {e.get("field").lower(): e.get("selected", "True") == "True" for e in entries if e.get("field") != "*Unknown"}
        unk = next((e for e in entries if e.get("field") == "*Unknown"), None)
        unk_sel = unk is not None and unk.get("selected", "True") == "True"
        data = [f for f in src.fields if f.name.lower() not in knames and explicit.get(f.name.lower(), unk_sel)]
        if not data:
            raise Unsupported("transpose has no data fields present in input")
        meta = t.meta.get("Output")
        vmeta = meta[-1] if meta and meta[-1].name == "Value" else None
        kinds = {f.kind for f in data}
        vtype = vmeta.atype if vmeta else ("Double" if kinds == {"num"} else "V_WString")
        parts = []
        for f in data:
            v = qid(f.name) if f.kind == kind_of(vtype) else cast_expr(qid(f.name), f.kind, vtype)
            parts.append(f"SELECT {self.cols(keys) + ', ' if keys else ''}{lit(f.name)} AS \"Name\", {v} AS \"Value\" FROM {src.cte}")
        sql = "\n    UNION ALL\n    ".join(parts)
        fields = list(keys) + [Field("Name", "V_WString"), Field("Value", vtype)]
        self.out[(t.id, "Output")] = Rel(self.add_cte(t, "", sql, [src]), fields)

    def m_dynamicrename(self, t: Tool):
        ins = self.inputs(t, "Input") or self.inputs(t)
        src = ins[0]
        mode = t.config.findtext("RenameMode")
        entries = t.config.findall("Fields/Field")
        explicit = {e.get("name").lower(): e.get("selected", "True") == "True" for e in entries if e.get("name") != "*Unknown"}
        unk = next((e for e in entries if e.get("name") == "*Unknown"), None)
        unk_sel = unk is not None and unk.get("selected", "True") == "True"
        targets = [f for f in src.fields if explicit.get(f.name.lower(), unk_sel)]
        if mode == "Formula":
            ex = t.config.findtext("Expression") or ""
            new = {}
            for f in targets:
                try:
                    v = eval_py(ex, {"_CurrentField_": f.name, "_CurrentFieldType_": f.atype})
                except Exception as e:  # noqa: BLE001
                    raise Unsupported(f"rename formula on {f.name}: {e}")
                new[f.name] = str(v)
        elif mode in ("Prefix", "Suffix", "AddPrefix", "AddSuffix", "RemovePrefix", "RemoveSuffix"):
            txt = t.config.findtext("Text") or t.config.findtext("PrefixSuffix") or ""
            new = {}
            for f in targets:
                n = f.name
                if mode in ("Prefix", "AddPrefix"):
                    n = txt + n
                elif mode in ("Suffix", "AddSuffix"):
                    n = n + txt
                elif mode == "RemovePrefix" and n.startswith(txt):
                    n = n[len(txt):]
                elif mode == "RemoveSuffix" and n.endswith(txt):
                    n = n[: -len(txt)]
                new[f.name] = n
        else:
            meta = t.meta.get("Output")
            if meta and len(meta) == len(src.fields):
                new = {a.name: b.name for a, b in zip(src.fields, meta) if a.name != b.name}
                self.issue(t, "WARN", f"rename mode {mode} is data-dependent; names frozen from Alteryx metadata")
            else:
                raise Unsupported(f"rename mode {mode} is data-dependent")
        sels, fields = [], []
        for f in src.fields:
            n = new.get(f.name, f.name)
            sels.append(qid(f.name) if n == f.name else f"{qid(f.name)} AS {qid(n)}")
            fields.append(Field(n, f.atype, f.size))
        sql = f"SELECT {', '.join(sels)} FROM {src.cte}"
        self.out[(t.id, "Output")] = Rel(self.add_cte(t, "", sql, [src]), fields, src.order)

    # ---------------------------------------------------------------- macros
    def m_macro_cleanse(self, t: Tool):
        (src,) = self.inputs(t)
        vals = {v.get("name"): (v.text or "") for v in t.config.iter("Value")}
        on = lambda n: vals.get(f"Check Box ({n})") == "True"  # noqa: E731
        listed = [x.strip().strip('"') for x in vals.get("List Box (11)", "").split('","')]
        listed = {x.strip('"').lower() for x in listed if x}
        sels = []
        for f in src.fields:
            x = qid(f.name)
            if f.name.lower() in listed:
                if f.kind == "str":
                    if on(84):
                        x = f"COALESCE({x}, '')"
                    if on(122):
                        x = f"REGEXP_REPLACE({x}, '\\\\s', '')"
                    else:
                        if on(109):
                            x = f"REGEXP_REPLACE(REGEXP_REPLACE({x}, '[\\\\t\\\\r\\\\n]', ''), ' {{2,}}', ' ')"
                        if on(15):
                            x = f"TRIM({x}, ' \\t\\r\\n')"
                    if on(53):
                        x = f"REGEXP_REPLACE({x}, '[A-Za-z]', '')"
                    if on(58):
                        x = f"REGEXP_REPLACE({x}, '[0-9]', '')"
                    if on(70):
                        x = f"REGEXP_REPLACE({x}, '[[:punct:]]', '')"
                    if on(77):
                        case = vals.get("Drop Down (81)", "upper").lower()
                        x = {"upper": f"UPPER({x})", "lower": f"LOWER({x})", "title": f"INITCAP({x})"}.get(case, x)
                elif f.kind == "num" and on(117):
                    x = f"COALESCE({x}, 0)"
            sels.append(x if x == qid(f.name) else f"{x} AS {qid(f.name)}")
        sql = f"SELECT {', '.join(sels)} FROM {src.cte}"
        rel = Rel(self.add_cte(t, "", sql, [src]), list(src.fields), src.order)
        for e in self.wf.outputs_of(t.id):
            self.out[(t.id, e.src_anchor)] = rel
        self.out[(t.id, "Output")] = rel

    # ---------------------------------------------------------------- terminals
    def m_output(self, t: Tool):
        (src,) = self.inputs(t)
        self.out[(t.id, "Output")] = src

    def m_browse(self, t: Tool):
        (src,) = self.inputs(t)
        self.out[(t.id, "Output")] = src

    def m_test(self, t: Tool):
        rels = {}
        for e in self.wf.inputs_of(t.id):
            r = self.out.get((e.src, e.src_anchor))
            if r is not None:
                rels[e.name] = r
        for test in t.config.findall("Tests/Test"):
            conn, typ, val, name = test.findtext("Connection"), test.findtext("Type"), test.findtext("Value"), test.findtext("Name")
            r = rels.get(conn)
            if r is None:
                self.issue(t, "TODO", f"test '{name}' input {conn} not connected")
                continue
            if typ == "ExpAll":
                cond, _, _ = to_sql(val, self.resolver(r))
                q = f"SELECT * FROM {{rel}} WHERE NOT ({cond})"
            elif typ in ("RecCount", "RecCountValue"):
                n = int(float(val or 0))
                # dbt fails a test when it returns rows; 0 expected -> return the offending records themselves
                q = (f"SELECT * FROM {{rel}}" if n == 0 else
                     f"SELECT COUNT(*) AS n FROM {{rel}} HAVING COUNT(*) <> {n}")
            elif typ == "ExpFirst":
                self.issue(t, "WARN", f"test '{name}': first-record test evaluated on every record")
                cond, _, _ = to_sql(val, self.resolver(r))
                q = f"SELECT * FROM {{rel}} WHERE NOT ({cond})"
            else:
                self.issue(t, "TODO", f"test '{name}' type {typ}")
                continue
            self.tests.append({"tool": t.id, "name": name, "rel": r, "sql": q})


def textinput_rows(t: Tool) -> tuple[list[str], list[list[str]]]:
    names = [x.get("name") for x in t.config.findall("Fields/Field")]
    rows = [[(c.text if c.text is not None else "") for c in r.findall("c")] for r in t.config.findall("Data/r")]
    return names, [r + [""] * (len(names) - len(r)) for r in rows]


def textinput_fields(t: Tool) -> list[Field]:
    """Cached Alteryx types when present; otherwise infer like the Text Input tool (numeric columns -> Double)."""
    if t.meta.get("Output"):
        return t.meta["Output"]
    names, rows = textinput_rows(t)
    out = []
    for i, n in enumerate(names):
        vals = [r[i] for r in rows if r[i] != ""]
        numeric = bool(vals) and all(re.fullmatch(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", v.strip()) for v in vals)
        out.append(Field(n, "Double" if numeric else "V_WString"))
    return out


def _walk(n):
    yield n
    for c in n[1:]:
        if isinstance(c, tuple):
            yield from _walk(c)
        elif isinstance(c, list):
            for x in c:
                if isinstance(x, tuple):
                    if x and isinstance(x[0], str):
                        yield from _walk(x)
                    else:
                        for y in x:
                            if isinstance(y, tuple):
                                yield from _walk(y)
