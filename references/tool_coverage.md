# Tool coverage

| Alteryx tool | Snowflake / dbt | Notes |
|---|---|---|
| Input Data (CSV, Excel sheet, Access .mdb, DB) | dbt source -> staging view casting to Alteryx types | Excel/Access: export to CSV and load to `RAW_ALTERYX` (DDL in `setup/01_landing_tables.sql`), or map to an existing table via source map |
| Text Input | dbt seed CSV + staging view | Business-maintained mapping tables stay editable |
| Select | column list with rename / cast / LEFT(size) | honours OrderChanged and `*Unknown` |
| Formula | `SELECT * REPLACE(...)`, new columns; new step when a field references one computed in the same tool | expression translator, see semantics.md |
| Filter (Basic + Custom) | WHERE / WHERE NOT on True/False anchors | null-safe |
| Join (L/J/R) | INNER JOIN + NOT EXISTS anti-joins, EQUAL_NULL keys | embedded select config applied |
| Append Fields | CROSS JOIN | |
| Union (by name / position / manual) | UNION ALL with explicit column lists and casts | |
| Summarize | GROUP BY; Sum, Count, CountNonNull, CountNull, CountDistinct, Min, Max, Avg, Concat, First/Last | First/Last/Concat order-dependent |
| Unique (U/D) | ROW_NUMBER partition | order from nearest upstream Sort |
| Sort | carried as ordering to Unique/First/Last and final SELECT | |
| Record ID | ROW_NUMBER | order-dependent |
| Cross Tab | conditional aggregation | columns frozen from cached metadata; TODO without it |
| Transpose | UNION ALL per data field | keeps Nulls |
| Dynamic Rename (Formula, prefix/suffix) | rename evaluated at conversion time | other modes frozen from cached metadata (WARN) or TODO |
| Test | ephemeral check model + dbt singular test | ExpAll, RecCount, RecCountValue |
| Browse / dead-end tools | `models/inspect` views | the analyst's checkpoints |
| Output Data | `models/marts` table + `setup/02_exports.sql` COPY INTO stage | confirm target format (e.g. Adaptive import) |
| Data Cleansing macro | COALESCE / TRIM / REGEXP_REPLACE / case | |
| Analytic app (.yxwz) | treated as a workflow | Question/Action tools that inject values are not yet mapped — flagged |

Not yet mapped (emitted as TODO pass-through): Multi-Row Formula, Multi-Field Formula, Generate Rows, Text To Columns, RegEx tool, Find Replace, Running Total, Sample, In-Database tools, Python/R/predictive, spatial, batch/iterative macros, Run Command, Download. Add a `m_<tooltype>` method in `scripts/a2s/mappers.py` and a case to `references/semantics.md`; keep the conformance suite green.
