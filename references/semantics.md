# Alteryx semantics the converter preserves (and the assumptions it makes)

Parity with today's Alteryx output is the acceptance test, so the generated SQL reproduces Alteryx behaviour even where it looks odd. Each **Verified** item is in the conformance suite (`a2s validate`) and passed on Snowflake; sources are Alteryx docs (help.alteryx.com) unless noted.

## Expressions
| Alteryx behaviour | SQL emitted | Status |
|---|---|---|
| String `=`, `!=`, `<`, `>` are case-insensitive | `UPPER(a) = UPPER(b)` | Verified |
| `Null = Null` is True; comparisons never yield Null | `EQUAL_NULL(a, b)`, `COALESCE(a < b, FALSE)` | Verified |
| `Null <= Null` is True | `COALESCE(a <= b, EQUAL_NULL(a, b))` | Verified |
| String + Null = String; Number + Null = Null | `COALESCE(s,'') \|\| ...` only when the expression is string-typed | Verified |
| `Round(x, mult)` rounds to nearest **multiple** (`Round(x, 2)` -> even numbers) | `ROUND(x, d)` when mult = 10^-d, else `ROUND(x/mult)*mult` | Verified |
| `Contains/StartsWith/EndsWith` default case-insensitive; Null -> False | `COALESCE(CONTAINS(UPPER(s), UPPER(t)), FALSE)` | Verified |
| `Substring`, `FindString` are 0-based; not found = -1 | `SUBSTR(s, i+1, n)`, `POSITION(t IN s) - 1` | Verified |
| `PadLeft/PadRight` never truncate (Snowflake LPAD/RPAD do) | `IFF(LENGTH(s) >= n, s, LPAD(...))` | Verified |
| Divide by zero -> Null | `a / NULLIF(b, 0)` | Verified |
| DateTimeParse output used as a string is `yyyy-MM-dd HH:mm:ss` | `TO_VARCHAR(ts, 'YYYY-MM-DD HH24:MI:SS')` | Verified |
| `Replace` is case-sensitive | `REPLACE` | Assumed (Alteryx docs) |
| String -> number conversion of bad values gives Null | `TRY_TO_DOUBLE` | **Assumption** — Alteryx may emit 0 with a conversion warning; check parity |

## Tools
| Behaviour | Implementation | Status |
|---|---|---|
| Join matches Null keys to Null keys | `EQUAL_NULL` in ON (Snowflake still plans a hash join on it — checked with EXPLAIN) | Doc + EXPLAIN |
| Join is case-sensitive | plain equality on raw values | Doc (community) |
| Join output prefixes a right field with `Right_` only when a same-named **selected** left field exists | implemented; validated 180/180 against cached schemas | Verified (schema check) |
| Summarize and Unique are case-sensitive; Count counts all records | `GROUP BY`, `COUNT(*)` | Doc |
| Unique keeps the *first* record per key in record order | `ROW_NUMBER() OVER (PARTITION BY keys ORDER BY <nearest upstream Sort>)` | WARN when no upstream Sort — kept row is arbitrary |
| Sort: Null lowest (first ascending) | `NULLS FIRST` asc / `NULLS LAST` desc | Doc |
| Text Input blank cells are empty strings; columns auto-typed | seed CSV loaded as VARCHAR, staging `COALESCE(x,'')`, numeric columns `TRY_TO_DOUBLE` | Assumption |
| Cross Tab header values become field names with invalid characters -> `_` | pivot on `REGEXP_REPLACE(header, '[^A-Za-z0-9_]', '_')`; column list frozen from cached metadata | WARN: new values need regeneration |
| Transpose keeps Null values | `UNION ALL` per data field (UNPIVOT would drop Nulls) | By design |
| Union "manual" mode | by position, names from input #1 | NOTE; validated by schema check |
| Data Cleansing macro options | checkbox ids 84/117 nulls, 15/109/122 whitespace, 53/58/70 characters, 77+81 case | Mapped from a labelled example workflow |
| Input "Output file name as field" | dbt var `<slug>_t<id>_filename` (default = file name) | Assumption: value format |

## Types
Staging casts every input column to the type **Alteryx read it as**. A CSV extract of a FLOAT column is a string in Alteryx; if the workflow then adds such fields with `+` it concatenates, and a Sort on a string-typed number is lexicographic. The converter reproduces this so outputs match. When moving to production you may *choose* native types — that is a logic change and needs its own parity run.
Number -> string conversions use Snowflake formatting (`TO_VARCHAR`), which can differ from the CSV text (`1.0` vs `1`); the parity script compares numbers at 6 dp to absorb this.

## Order dependence
SQL has no record order. Tools whose result depends on it (Unique without upstream Sort, First/Last, RecordID, Concatenate) are flagged WARN. Fix by adding an explicit sort key that encodes the intended business rule (e.g. latest date wins).
