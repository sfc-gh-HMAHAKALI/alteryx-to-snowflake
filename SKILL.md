---
name: alteryx-to-snowflake
description: "Convert Alteryx workflows (.yxmd, .yxwz analytic apps, .yxmc macros) into Snowflake dbt projects with deterministic Python (no LLM per tool), then prove the result with synthetic data, semantics conformance tests, per-tool row-count invariants and an Alteryx-vs-Snowflake parity script. Use whenever the user mentions Alteryx, yxmd, yxwz, Alteryx Designer/Server replacement, Alteryx renewal, migrating Alteryx transformations or ETL to Snowflake, dbt, or 'what does this Alteryx workflow do' — even if they only ask to analyse or inventory Alteryx files. SnowConvert does not support Alteryx; this skill does."
---

# Alteryx -> Snowflake (dbt)

Deterministic converter: the Alteryx XML is parsed, every tool is mapped to a SQL CTE by Python, and the LLM is only needed for the few constructs flagged `TODO(ALTERYX:<tool id>)`. This keeps token cost flat no matter how many workflows there are, and makes output reproducible run-to-run.

## When to Use
- Inventory / complexity-score a set of Alteryx workflows
- Convert workflows to dbt projects that run as **dbt Projects on Snowflake** (`snow dbt deploy/execute`)
- Validate conversions (synthetic data in a sandbox, then parity against real Alteryx output)

Not for: Alteryx Server administration, predictive/R/Python tools (flagged as TODO; port to Snowpark by hand).

## Prerequisites
- Python 3.10+ (standard library only — no installs)
- `snow` CLI with a connection that can create a database/schema (for deploy + validate)
- CLI entry point (use absolute paths; the package lives in `scripts/`):
  ```bash
  PYTHONPATH=<SKILL_DIR>/scripts python3 -m a2s <command> ...
  ```
  Every command prints JSON to stdout.

## Workflow

### Step 1: Inventory
```bash
PYTHONPATH=<SKILL_DIR>/scripts python3 -m a2s inventory <files...>
```
Returns per workflow: tool counts, complexity score, `unsupported_tools`, sources (file paths, sheets, field lists), sinks, Text Input mapping tables, containers.
Summarise for the user: which workflows read files vs databases, where outputs go, unsupported tools.

### Step 2: Ground inputs in Snowflake (source map)
Alteryx inputs are often CSV/Excel *extracts of Snowflake tables*. Matching them back to the real table lets dbt read the live table instead of a file.
- Match each input's field list against `SNOWFLAKE.ACCOUNT_USAGE.COLUMNS` (or `<db>.INFORMATION_SCHEMA.COLUMNS`) by column-name overlap. A table containing 100% of an input's fields is a near-certain match.
- Write `source_map.json`:
  ```json
  {"<workflow name>#<input tool id>": {"database": "DB", "schema": "SCH", "table": "TBL",
                                        "columns": {"COL": "FLOAT", "COL2": "VARCHAR(40)"}}}
  ```
  Unmapped inputs become landing tables in `RAW_ALTERYX` (all VARCHAR) — see `setup/01_landing_tables.sql` in each project.

**⚠️ STOPPING POINT:** show the proposed input -> table mapping with match % and get confirmation. A wrong mapping silently changes results.

### Step 3: Convert
```bash
PYTHONPATH=<SKILL_DIR>/scripts python3 -m a2s convert <files...> -o <out_dir> \
  --source-map source_map.json --slug-map slugs.json \
  [--target-role R --target-warehouse W --target-database D --target-schema S]
```
- `slugs.json` (`{"<workflow name>": "short_name"}`) keeps model names short; models are prefixed `<slug>__`.
- `--target-*` writes `profiles.yml`. dbt on Snowflake runs as the caller, so **role must equal the role that executes the project** (otherwise "USE ROLE not allowed").
- Output per workflow: `models/staging` (one view per input, cast to the type Alteryx read), `seeds/` (Text Input tools as editable CSV), `models/marts` (Output tools, tables), `models/inspect` (Browse tools and dead-end tools — the analyst's checkpoints, views), `models/checks` + `tests/` (Alteryx Test tools -> dbt tests), `setup/` (landing DDL, egress COPY, task schedule), `parity/`, `CONVERSION.md`, `conversion_report.json`.

### Step 4: Review issues
Read `CONVERSION.md`. Severity meanings:
- **TODO** — not translated; a pass-through placeholder CTE was emitted. Fix by editing that CTE (this is where the LLM is useful). Then re-run Step 6.
- **WARN** — translated, but behaviour may differ (record-order dependence, frozen pivot columns, duplicate field names).
- **NOTE** — informational.
`schema_check` compares every tool's output fields with Alteryx's cached metadata (present in some files, e.g. analytic apps); mismatches mean a mapper bug — investigate before trusting results.

Load `references/semantics.md` when explaining *why* SQL looks unusual (UPPER on comparisons, EQUAL_NULL joins, string-typed numbers) or when a parity diff appears.
Load `references/tool_coverage.md` for what each tool maps to and the known gaps.

### Step 5: (Optional) Synthetic data + sandbox
```bash
PYTHONPATH=<SKILL_DIR>/scripts python3 -m a2s synth <files...> -o <synth_dir> \
  --source-map source_map.json --slug-map slugs.json --row-counts row_counts.json
```
Values are harvested from the workflow (filter/formula literals, Text Input mapping values, join-key groups; self-joined hierarchies such as BOMs get multi-level chains), so joins match and filters fire. Each `<synth_dir>/<slug>/load.sql` creates the tables, PUTs and COPYs the CSVs. Run with `snow sql -c <conn> --database <sandbox_db> -f load.sql` after creating schemas `TRANSFORM`/`RAW_ALTERYX` (or whatever the source map uses) and stage `RAW_ALTERYX.SYNTH_STAGE`.

Deploy and build:
```bash
cd <out_dir>/<slug> && snow dbt deploy <SLUG> -c <conn> --database <db> --schema <schema> --source .
snow dbt execute -c <conn> --database <db> --schema <schema> <SLUG> build
```
A failing dbt test that came from an Alteryx Test tool is a *data* finding (e.g. unmapped customers), not a conversion bug.

### Step 6: Validate
```bash
PYTHONPATH=<SKILL_DIR>/scripts python3 -m a2s validate <files...> -c <conn> \
  --database <db> --schema <schema> --source-map source_map.json --slug-map slugs.json -o <dir>
```
Runs in Snowflake: (1) 26-case semantics conformance suite (Round-to-multiple, null-safe/case-insensitive comparison, 0-based Substring, …); (2) row counts for every CTE checked against per-tool invariants (Filter T+F = input, Join L+matched = left, Unique U+D = input, Union = sum, Select/Formula preserve rows, Transpose = input × fields); (3) coverage: tools that received rows. Exit code 0 = all invariants hold.
Zero-row tools are listed with a reason: Alteryx Filters annotated "should be 0 records" are data-quality gates (expected empty); others mean synthetic data didn't reach that path — real-data parity covers them.

### Step 7: Parity on real data (customer-run)
`parity/<model>.sql` per output. The customer runs the Alteryx workflow and the dbt project on the **same input files**, loads the Alteryx output to `<db>.ALTERYX_BASELINE.<MODEL>`, and runs the script: (a) row counts, (b) rows only on one side (duplicate-aware), (c) per-column hash (meaningful when counts match). Numbers compare at 6 dp; empty string = NULL (Excel loses the distinction).
Tested: identical copy -> 0 diffs; one value +0.5 and one row deleted -> both reported.

**⚠️ STOPPING POINT:** report parity results before recommending cut-over.

## Stopping Points
- ✋ Step 2: confirm input -> Snowflake table mapping
- ✋ Step 4: if any TODO, agree who fixes it before deploying
- ✋ Step 7: parity results before cut-over

## Output
dbt project per workflow + `CONVERSION.md`, validation report JSON, parity scripts.

## Troubleshooting
- `USE ROLE not allowed` on deploy: profiles.yml role ≠ executing role -> re-run convert with `--target-role`.
- `invalid identifier` inside a model: a field was dropped upstream; check `schema_check` and the mapper for that tool type.
- `snow sql` templating errors on `&`/`{`: add `--enable-templating NONE`.
- Objects not found by uppercase name: projects set `quoting: identifier: false`; older builds may have created lowercase relations — drop and rebuild.
