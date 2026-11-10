"""Alteryx formula language -> Snowflake SQL (and a small Python evaluator).

Semantics that differ from SQL and are preserved here (see references/semantics.md):
  * String `=`, `!=`, `<`, `>` are case-INSENSITIVE in Alteryx            -> UPPER() both sides
  * Null = Null is TRUE; comparisons never return Null                    -> EQUAL_NULL / COALESCE(.., FALSE)
  * String + Null = String; Number + Null = Null                          -> COALESCE(str, '') when concatenating
  * Contains / StartsWith / EndsWith default to case-insensitive          -> UPPER() both sides
  * Round(x, mult) rounds to the nearest MULTIPLE, not to N decimals      -> ROUND(x/mult)*mult (or ROUND(x, d) for 10^-d)
  * PadLeft/PadRight never truncate (Snowflake LPAD/RPAD do)              -> guarded with LENGTH check
  * Substring / FindString are 0-based                                    -> +1 / -1 adjustments
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass


class Unsupported(Exception):
    """Raised for constructs we do not translate deterministically (emitted as TODO)."""


# ---------------------------------------------------------------- tokenizer
TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<field>\[[^\]]*\])
  | (?P<str>"(?:[^"]|"")*"|'(?:[^']|'')*')
  | (?P<num>\d+\.\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|\d+(?:[eE][+-]?\d+)?)
  | (?P<op><=|>=|!=|<>|==|=|<|>|\+|-|\*|/|%|!|\(|\)|,)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_.]*)
""", re.X | re.S)

KEYWORDS = {"IF", "THEN", "ELSEIF", "ELSE", "ENDIF", "AND", "OR", "NOT", "IN", "TRUE", "FALSE"}


@dataclass
class Tok:
    kind: str
    val: str


def tokenize(s: str) -> list[Tok]:
    pos, out = 0, []
    while pos < len(s):
        m = TOKEN_RE.match(s, pos)
        if not m:
            raise Unsupported(f"cannot tokenize near: {s[pos:pos + 20]!r}")
        pos = m.end()
        k = m.lastgroup
        v = m.group(k)
        if k in ("ws", "comment"):
            continue
        if k == "ident" and v.upper() in KEYWORDS:
            out.append(Tok("kw", v.upper()))
        elif k == "str":
            q = v[0]
            out.append(Tok("str", v[1:-1].replace(q + q, q)))
        else:
            out.append(Tok(k, v))
    out.append(Tok("eof", ""))
    return out


# ---------------------------------------------------------------- parser -> AST (tuples)
class Parser:
    def __init__(self, s: str):
        self.t = tokenize(s)
        self.i = 0

    def peek(self, k=None, v=None):
        t = self.t[self.i]
        return (k is None or t.kind == k) and (v is None or t.val.upper() == v)

    def take(self, k=None, v=None):
        if not self.peek(k, v):
            got = self.t[self.i]
            raise Unsupported(f"expected {k} {v or ''} got {got.kind} {got.val!r}")
        tok = self.t[self.i]
        self.i += 1
        return tok

    def parse(self):
        e = self.expr()
        self.take("eof")
        return e

    def expr(self):
        return self.or_()

    def or_(self):
        a = self.and_()
        while self.peek("kw", "OR"):
            self.i += 1
            a = ("or", a, self.and_())
        return a

    def and_(self):
        a = self.not_()
        while self.peek("kw", "AND"):
            self.i += 1
            a = ("and", a, self.not_())
        return a

    def not_(self):
        if self.peek("kw", "NOT") or self.peek("op", "!"):
            self.i += 1
            return ("not", self.not_())
        return self.cmp()

    def cmp(self):
        a = self.add()
        if self.peek("op") and self.t[self.i].val in ("=", "==", "!=", "<>", "<", ">", "<=", ">="):
            op = self.take().val
            op = {"==": "=", "<>": "!="}.get(op, op)
            return ("cmp", op, a, self.add())
        neg = False
        if self.peek("kw", "NOT") and self.t[self.i + 1].kind == "kw" and self.t[self.i + 1].val == "IN":
            self.i += 1
            neg = True
        if self.peek("kw", "IN"):
            self.i += 1
            self.take("op", "(")
            items = [self.expr()]
            while self.peek("op", ","):
                self.i += 1
                items.append(self.expr())
            self.take("op", ")")
            return ("in", a, items, neg)
        return a

    def add(self):
        a = self.mul()
        while self.peek("op") and self.t[self.i].val in ("+", "-"):
            op = self.take().val
            a = ("arith", op, a, self.mul())
        return a

    def mul(self):
        a = self.unary()
        while self.peek("op") and self.t[self.i].val in ("*", "/", "%"):
            op = self.take().val
            a = ("arith", op, a, self.unary())
        return a

    def unary(self):
        if self.peek("op", "-"):
            self.i += 1
            return ("neg", self.unary())
        if self.peek("op", "+"):
            self.i += 1
            return self.unary()
        return self.primary()

    def primary(self):
        t = self.t[self.i]
        if t.kind == "num":
            self.i += 1
            return ("num", t.val)
        if t.kind == "str":
            self.i += 1
            return ("str", t.val)
        if t.kind == "field":
            self.i += 1
            return ("field", t.val[1:-1])
        if t.kind == "kw" and t.val in ("TRUE", "FALSE"):
            self.i += 1
            return ("bool", t.val == "TRUE")
        if t.kind == "kw" and t.val == "IF":
            return self.if_()
        if t.kind == "op" and t.val == "(":
            self.i += 1
            e = self.expr()
            self.take("op", ")")
            return e
        if t.kind == "ident":
            self.i += 1
            name = t.val
            if not self.peek("op", "("):
                raise Unsupported(f"bare identifier {name!r} (engine constant?)")
            self.i += 1
            args = []
            if not self.peek("op", ")"):
                args.append(self.expr())
                while self.peek("op", ","):
                    self.i += 1
                    args.append(self.expr())
            self.take("op", ")")
            return ("call", name.lower(), args)
        raise Unsupported(f"unexpected token {t.kind} {t.val!r}")

    def if_(self):
        self.take("kw", "IF")
        branches = []
        c = self.expr()
        self.take("kw", "THEN")
        branches.append((c, self.expr()))
        while self.peek("kw", "ELSEIF"):
            self.i += 1
            c = self.expr()
            self.take("kw", "THEN")
            branches.append((c, self.expr()))
        els = None
        if self.peek("kw", "ELSE"):
            self.i += 1
            els = self.expr()
        self.take("kw", "ENDIF")
        return ("if", branches, els)


def parse(expr: str):
    return Parser(expr).parse()


# ---------------------------------------------------------------- SQL backend
def qid(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def lit(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "''") + "'"


DT_FMT = [("%Y", "YYYY"), ("%y", "YY"), ("%m", "MM"), ("%d", "DD"), ("%H", "HH24"), ("%I", "HH12"),
          ("%M", "MI"), ("%S", "SS"), ("%B", "MMMM"), ("%b", "MON"), ("%h", "MON"), ("%A", "DY"),
          ("%a", "DY"), ("%p", "AM"), ("%e", "DD"), ("%j", "DDD")]


def dt_format(f: str) -> str:
    out = f
    for a, b in DT_FMT:
        out = out.replace(a, b)
    if "%" in out:
        raise Unsupported(f"datetime format {f!r}")
    return out


# kinds: num, str, bool, date, null
class SQLGen:
    """Translate an AST to Snowflake SQL given a resolver for field references.

    resolve(name) -> (sql_expr, kind)  -- raises KeyError when the field is unknown.
    """

    def __init__(self, resolve):
        self.resolve = resolve
        self.notes: set[str] = set()

    def gen(self, n) -> tuple[str, str]:
        k = n[0]
        if k == "num":
            return n[1], "num"
        if k == "str":
            return lit(n[1]), "str"
        if k == "bool":
            return ("TRUE" if n[1] else "FALSE"), "bool"
        if k == "field":
            try:
                return self.resolve(n[1])
            except KeyError:
                raise Unsupported(f"unknown field [{n[1]}]")
        if k == "and":
            return f"({self.b(n[1])} AND {self.b(n[2])})", "bool"
        if k == "or":
            return f"({self.b(n[1])} OR {self.b(n[2])})", "bool"
        if k == "not":
            return f"(NOT {self.b(n[1])})", "bool"
        if k == "neg":
            s, _ = self.gen(n[1])
            return f"(-{s})", "num"
        if k == "cmp":
            return self.cmp(n[1], n[2], n[3]), "bool"
        if k == "in":
            a, ak = self.gen(n[1])
            parts = [self.cmp_sql("=", a, ak, *self.gen(x)) for x in n[2]]
            s = "(" + " OR ".join(parts) + ")"
            return (f"(NOT {s})" if n[3] else s), "bool"
        if k == "arith":
            return self.arith(n[1], n[2], n[3])
        if k == "if":
            parts, kinds = [], []
            for c, v in n[1]:
                vs, vk = self.gen(v)
                kinds.append(vk)
                parts.append(f"WHEN {self.b(c)} THEN {vs}")
            es, ek = self.gen(n[2]) if n[2] is not None else ("NULL", "null")
            kinds.append(ek)
            kind = _merge_kinds(kinds)
            return "CASE " + " ".join(parts) + f" ELSE {es} END", kind
        if k == "call":
            return self.call(n[1], n[2])
        raise Unsupported(f"node {k}")

    # boolean context: Alteryx booleans are never Null
    def b(self, n) -> str:
        s, kind = self.gen(n)
        if kind == "bool":
            safe = n[0] in ("cmp", "and", "or", "not", "in", "bool") or (n[0] == "call" and n[1] in NULL_SAFE_BOOL_FNS)
            return s if safe else f"COALESCE({s}, FALSE)"
        if kind == "num":
            return f"COALESCE({s} <> 0, FALSE)"
        raise Unsupported("non-boolean expression used as condition")

    def as_str(self, s, kind):
        if kind == "date":
            return f"TO_VARCHAR({s}, 'YYYY-MM-DD HH24:MI:SS')"
        if kind == "num":
            self.notes.add("number-to-string conversion uses Snowflake default formatting")
            return f"TO_VARCHAR({s})"
        if kind == "bool":
            return f"IFF({s}, 'True', 'False')"
        return s

    def cmp_sql(self, op, a, ak, b, bk):
        strish = "str" in (ak, bk) and not ({ak, bk} & {"num"})
        if ak == "date" and bk == "str":
            a = self.as_str(a, ak)
        if bk == "date" and ak == "str":
            b = self.as_str(b, bk)
        if strish:
            a, b = f"UPPER({a})", f"UPPER({b})"
        elif "str" in (ak, bk) and "num" in (ak, bk):
            # Alteryx raises a type error here; mirror the most common intent (numeric compare)
            self.notes.add("string/number comparison coerced with TRY_TO_DOUBLE")
            a = f"TRY_TO_DOUBLE({a})" if ak == "str" else a
            b = f"TRY_TO_DOUBLE({b})" if bk == "str" else b
        if op == "=":
            return f"EQUAL_NULL({a}, {b})"
        if op == "!=":
            return f"(NOT EQUAL_NULL({a}, {b}))"
        if op in ("<=", ">="):
            # Null <= Null is TRUE in Alteryx
            return f"COALESCE({a} {op} {b}, EQUAL_NULL({a}, {b}))"
        return f"COALESCE({a} {op} {b}, FALSE)"

    def cmp(self, op, x, y):
        a, ak = self.gen(x)
        b, bk = self.gen(y)
        return self.cmp_sql(op, a, ak, b, bk)

    def arith(self, op, x, y):
        a, ak = self.gen(x)
        b, bk = self.gen(y)
        if op == "+" and ("str" in (ak, bk) or "date" in (ak, bk)) and not (ak == "num" and bk == "num"):
            # String + Null = String (Null + Null stays Null)
            nx, ny = _nonnull(x), _nonnull(y)
            a2 = a if nx else f"COALESCE({self.as_str(a, ak)}, '')"
            b2 = b if ny else f"COALESCE({self.as_str(b, bk)}, '')"
            if not nx and not ny:
                return f"IFF({a} IS NULL AND {b} IS NULL, NULL, {a2} || {b2})", "str"
            return f"({a2} || {b2})", "str"
        if op == "/":
            # Alteryx: divide by zero -> Null (with a warning)
            return f"({a} / NULLIF({b}, 0))", "num"
        if op == "%":
            return f"MOD({a}, {b})", "num"
        return f"({a} {op} {b})", "num"

    # ------------------------------------------------------------ functions
    def call(self, fn, args):
        g = [self.gen(a) for a in args]
        S = [s for s, _ in g]
        K = [k for _, k in g]

        def s(i):
            return self.as_str(S[i], K[i])

        def ci_flag(i, default=1):
            if len(args) <= i:
                return default
            a = args[i]
            if a[0] == "num":
                return int(float(a[1]))
            if a[0] == "bool":
                return int(a[1])
            raise Unsupported(f"{fn}: non-literal case flag")

        if fn == "null":
            return "NULL", "null"
        if fn == "isnull":
            return f"({S[0]} IS NULL)", "bool"
        if fn == "isempty":
            return f"({S[0]} IS NULL OR {s(0)} = '')", "bool"
        if fn == "iif":
            return f"IFF({self.b(args[0])}, {S[1]}, {S[2]})", _merge_kinds(K[1:3])
        if fn == "contains":
            if ci_flag(2):
                return f"COALESCE(CONTAINS(UPPER({s(0)}), UPPER({s(1)})), FALSE)", "bool"
            return f"COALESCE(CONTAINS({s(0)}, {s(1)}), FALSE)", "bool"
        if fn in ("startswith", "endswith"):
            f = "STARTSWITH" if fn == "startswith" else "ENDSWITH"
            if ci_flag(2):
                return f"COALESCE({f}(UPPER({s(0)}), UPPER({s(1)})), FALSE)", "bool"
            return f"COALESCE({f}({s(0)}, {s(1)}), FALSE)", "bool"
        if fn == "left":
            return f"LEFT({s(0)}, {S[1]})", "str"
        if fn == "right":
            return f"RIGHT({s(0)}, {S[1]})", "str"
        if fn == "substring":
            if len(args) == 2:
                return f"SUBSTR({s(0)}, ({S[1]}) + 1)", "str"
            return f"SUBSTR({s(0)}, ({S[1]}) + 1, {S[2]})", "str"
        if fn == "length":
            return f"LENGTH({s(0)})", "num"
        if fn == "uppercase":
            return f"UPPER({s(0)})", "str"
        if fn == "lowercase":
            return f"LOWER({s(0)})", "str"
        if fn == "titlecase":
            return f"INITCAP({s(0)})", "str"
        if fn in ("trim", "trimleft", "trimright"):
            f = {"trim": "TRIM", "trimleft": "LTRIM", "trimright": "RTRIM"}[fn]
            return (f"{f}({s(0)})" if len(S) == 1 else f"{f}({s(0)}, {s(1)})"), "str"
        if fn == "replace":
            # Alteryx Replace is case-sensitive (documented); Snowflake REPLACE is too
            return f"REPLACE({s(0)}, {s(1)}, {s(2)})", "str"
        if fn == "replacechar":
            return f"TRANSLATE({s(0)}, {s(1)}, REPEAT({s(2)}, LENGTH({s(1)})))", "str"
        if fn in ("padleft", "padright"):
            f = "LPAD" if fn == "padleft" else "RPAD"
            return f"IFF(LENGTH({s(0)}) >= {S[1]}, {s(0)}, {f}({s(0)}, {S[1]}, {s(2)}))", "str"
        if fn == "findstring":
            return f"(POSITION({s(1)} IN {s(0)}) - 1)", "num"
        if fn == "reversestring":
            return f"REVERSE({s(0)})", "str"
        if fn == "countwords":
            return f"ARRAY_SIZE(SPLIT(TRIM(REGEXP_REPLACE({s(0)}, '\\\\s+', ' ')), ' '))", "num"
        if fn == "getword":
            return f"SPLIT_PART(TRIM({s(0)}), ' ', ({S[1]}) + 1)", "str"
        if fn == "regex_match":
            icase = ci_flag(2, 1)
            return f"REGEXP_LIKE({s(0)}, {S[1]}{', ' + lit('i') if icase else ''})", "bool"
        if fn == "regex_replace":
            icase = ci_flag(3, 1)
            return f"REGEXP_REPLACE({s(0)}, {S[1]}, {S[2]}, 1, 0{', ' + lit('i') if icase else ''})", "str"
        if fn == "tonumber":
            return f"TRY_TO_DOUBLE({s(0)})", "num"
        if fn == "tostring":
            if len(S) > 1:
                return f"TO_VARCHAR(ROUND({S[0]}, {S[1]}))", "str"
            return self.as_str(S[0], K[0]), "str"
        if fn == "round":
            m = args[1]
            if m[0] == "num":
                mult = float(m[1])
                if mult > 0:
                    d = -math.log10(mult)
                    if abs(d - round(d)) < 1e-9:
                        return f"ROUND({S[0]}, {int(round(d))})", "num"
                    return f"(ROUND({S[0]} / {m[1]}) * {m[1]})", "num"
            return f"(ROUND({S[0]} / ({S[1]})) * ({S[1]}))", "num"
        if fn in ("ceil", "floor"):
            f = fn.upper()
            if len(S) == 2:
                return f"({f}({S[0]} / ({S[1]})) * ({S[1]}))", "num"
            return f"{f}({S[0]})", "num"
        if fn in ("abs", "sqrt", "exp", "sign"):
            return f"{fn.upper()}({S[0]})", "num"
        if fn == "log":
            return f"LN({S[0]})", "num"
        if fn == "log10":
            return f"LOG(10, {S[0]})", "num"
        if fn == "pow":
            return f"POWER({S[0]}, {S[1]})", "num"
        if fn in ("min", "max"):
            f = "LEAST" if fn == "min" else "GREATEST"
            return f"{f}({', '.join(S)})", _merge_kinds(K)
        if fn == "switch":
            val, default = S[0], S[1]
            pairs = [(S[i], S[i + 1]) for i in range(2, len(S) - 1, 2)]
            whens = " ".join(f"WHEN EQUAL_NULL({val}, {c}) THEN {r}" for c, r in pairs)
            return f"CASE {whens} ELSE {default} END", _merge_kinds([K[1]] + [K[i + 1] for i in range(2, len(S) - 1, 2)])
        if fn == "datetimeparse":
            fmt = args[1]
            if fmt[0] != "str":
                raise Unsupported("DateTimeParse with non-literal format")
            f = dt_format(fmt[1])
            if "YY" not in f:
                # Only safe when downstream uses the parts that were parsed (e.g. the month)
                self.notes.add(f"DateTimeParse format {fmt[1]!r} has no year; Snowflake defaults the year to 1970")
            return f"TRY_TO_TIMESTAMP({s(0)}, {lit(f)})", "date"
        if fn == "datetimeformat":
            fmt = args[1]
            if fmt[0] != "str":
                raise Unsupported("DateTimeFormat with non-literal format")
            return f"TO_VARCHAR({S[0]}, {lit(dt_format(fmt[1]))})", "str"
        if fn == "datetimetoday":
            return "CURRENT_DATE()::TIMESTAMP_NTZ", "date"
        if fn == "datetimenow":
            return "CURRENT_TIMESTAMP()::TIMESTAMP_NTZ", "date"
        if fn == "datetimefirstofmonth":
            return "DATE_TRUNC('month', CURRENT_DATE())::TIMESTAMP_NTZ", "date"
        if fn == "datetimeadd":
            unit = args[2]
            if unit[0] != "str":
                raise Unsupported("DateTimeAdd with non-literal unit")
            return f"DATEADD({_dt_unit(unit[1])}, {S[1]}, {self.as_date(S[0], K[0])})", "date"
        if fn == "datetimediff":
            unit = args[2]
            if unit[0] != "str":
                raise Unsupported("DateTimeDiff with non-literal unit")
            # Alteryx: DateTimeDiff(dt1, dt2, unit) = dt1 - dt2
            return f"DATEDIFF({_dt_unit(unit[1])}, {self.as_date(S[1], K[1])}, {self.as_date(S[0], K[0])})", "num"
        if fn == "datetimetrim":
            unit = args[1]
            if unit[0] != "str":
                raise Unsupported("DateTimeTrim with non-literal unit")
            u = unit[1].lower()
            if u in ("firstofmonth", "month"):
                return f"DATE_TRUNC('month', {self.as_date(S[0], K[0])})", "date"
            if u in ("year",):
                return f"DATE_TRUNC('year', {self.as_date(S[0], K[0])})", "date"
            if u in ("day",):
                return f"DATE_TRUNC('day', {self.as_date(S[0], K[0])})", "date"
            if u == "lastofmonth":
                return f"LAST_DAY({self.as_date(S[0], K[0])})::TIMESTAMP_NTZ", "date"
            raise Unsupported(f"DateTimeTrim unit {u}")
        if fn in ("datetimeyear", "datetimemonth", "datetimeday", "datetimehour", "datetimeminutes"):
            part = {"datetimeyear": "year", "datetimemonth": "month", "datetimeday": "day",
                    "datetimehour": "hour", "datetimeminutes": "minute"}[fn]
            return f"DATE_PART({part}, {self.as_date(S[0], K[0])})", "num"
        raise Unsupported(f"function {fn}()")

    def as_date(self, s, k):
        return s if k == "date" else f"TRY_TO_TIMESTAMP({s})"


NULL_SAFE_BOOL_FNS = {"isnull", "isempty", "contains", "startswith", "endswith"}


def _nonnull(n) -> bool:
    """True when the node can never evaluate to Null (literals and concatenations involving one)."""
    if n[0] in ("str", "num", "bool"):
        return True
    if n[0] == "arith" and n[1] == "+":
        return _nonnull(n[2]) or _nonnull(n[3])
    return False


def _dt_unit(u: str) -> str:
    u = u.lower().rstrip("s")
    m = {"year": "year", "month": "month", "day": "day", "hour": "hour", "minute": "minute", "min": "minute",
         "second": "second", "sec": "second", "week": "week"}
    if u not in m:
        raise Unsupported(f"datetime unit {u}")
    return m[u]


def _merge_kinds(kinds):
    ks = {k for k in kinds if k != "null"}
    if not ks:
        return "null"
    if len(ks) == 1:
        return ks.pop()
    if ks <= {"num", "bool"}:
        return "num"
    return "str"


def to_sql(expr: str, resolve) -> tuple[str, str, set[str]]:
    g = SQLGen(resolve)
    sql, kind = g.gen(parse(expr))
    return sql, kind, g.notes


# ---------------------------------------------------------------- Python evaluator (constant folding)
def eval_py(expr: str, env: dict):
    """Evaluate an expression on literal values (used for DynamicRename on field names)."""
    return _ev(parse(expr), {k.lower(): v for k, v in env.items()})


def _ev(n, env):
    k = n[0]
    if k == "num":
        return float(n[1])
    if k == "str":
        return n[1]
    if k == "bool":
        return n[1]
    if k == "field":
        return env[n[1].lower()]
    if k == "arith":
        a, b = _ev(n[2], env), _ev(n[3], env)
        if n[1] == "+" and (isinstance(a, str) or isinstance(b, str)):
            return (a or "") + (b or "")
        return {"+": a + b, "-": a - b, "*": a * b, "/": a / b if b else None}[n[1]]
    if k == "cmp":
        a, b = _ev(n[2], env), _ev(n[3], env)
        if isinstance(a, str) and isinstance(b, str):
            a, b = a.upper(), b.upper()
        return {"=": a == b, "!=": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[n[1]]
    if k == "and":
        return _ev(n[1], env) and _ev(n[2], env)
    if k == "or":
        return _ev(n[1], env) or _ev(n[2], env)
    if k == "not":
        return not _ev(n[1], env)
    if k == "if":
        for c, v in n[1]:
            if _ev(c, env):
                return _ev(v, env)
        return _ev(n[2], env) if n[2] is not None else None
    if k == "call":
        fn, a = n[1], [_ev(x, env) for x in n[2]]
        if fn == "left":
            return a[0][: int(a[1])]
        if fn == "right":
            return a[0][-int(a[1]):] if int(a[1]) else ""
        if fn == "substring":
            st = int(a[1])
            return a[0][st: st + int(a[2])] if len(a) > 2 else a[0][st:]
        if fn == "length":
            return float(len(a[0]))
        if fn == "uppercase":
            return a[0].upper()
        if fn == "lowercase":
            return a[0].lower()
        if fn == "trim":
            return a[0].strip() if len(a) == 1 else a[0].strip(a[1])
        if fn == "replace":
            return a[0].replace(a[1], a[2])
        if fn == "contains":
            return a[1].upper() in a[0].upper()
        if fn == "padleft":
            return a[0].rjust(int(a[1]), a[2])
        if fn == "padright":
            return a[0].ljust(int(a[1]), a[2])
        if fn == "titlecase":
            return a[0].title()
        if fn == "regex_replace":
            return re.sub(a[1], a[2], a[0], flags=re.I if (len(a) < 4 or a[3]) else 0)
        raise Unsupported(f"eval {fn}()")
    raise Unsupported(f"eval {k}")
