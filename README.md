# alteryx-to-snowflake

A [Cortex Code](https://docs.snowflake.com/) skill that converts Alteryx workflows (`.yxmd`, `.yxwz`) into
[dbt Projects on Snowflake](https://docs.snowflake.com/), then validates the result.

- **Deterministic:** the Alteryx XML is parsed and each tool is mapped to SQL by Python rules. No LLM call per tool;
  the same input always produces the same output. Constructs it cannot translate are flagged `TODO(ALTERYX:<tool id>)`.
- **Faithful to Alteryx semantics:** case-insensitive string comparison, `Null = Null`, `Round(x, mult)`, null-safe
  joins, 0-based `Substring`, and more. See [`references/semantics.md`](references/semantics.md).
- **Validated:** semantics conformance suite and per-tool row-count invariants run in Snowflake (`a2s validate`),
  plus a parity script per output to compare against the original Alteryx results.

## Install

```bash
git clone https://github.com/sfc-gh-HMAHAKALI/alteryx-to-snowflake ~/.snowflake/cortex/skills/alteryx-to-snowflake
```

Requires Python 3.10+ (standard library only) and the [Snowflake CLI](https://docs.snowflake.com/en/developer-guide/snowflake-cli/index) for deploy/validate.
In Cortex Code, ask: *"Convert these Alteryx files with the alteryx-to-snowflake skill."*

## Use from the command line

```bash
export PYTHONPATH=~/.snowflake/cortex/skills/alteryx-to-snowflake/scripts
python3 -m a2s inventory path/to/*.yxmd
python3 -m a2s convert path/to/*.yx* -o dbt_projects --source-map source_map.json \
  --target-role <ROLE> --target-warehouse <WH> --target-database <DB> --target-schema ALTERYX_MIGRATED
cd dbt_projects/<project> && snow dbt deploy <PROJECT> --source . && snow dbt execute <PROJECT> build
python3 -m a2s validate path/to/*.yx* -c <connection> --database <DB> --schema ALTERYX_MIGRATED -o validation
```

Full workflow: [`SKILL.md`](SKILL.md). Tool coverage and known gaps: [`references/tool_coverage.md`](references/tool_coverage.md).

## Tests

```bash
PYTHONPATH=scripts python3 -m unittest discover -s tests
```

## Repository Owner

- **Owner:** Himavarsh Mahakali (himavarsh.mahakali@snowflake.com / [@sfc-gh-HMAHAKALI](https://github.com/sfc-gh-HMAHAKALI))
- **Access requests:** Email the owner, or open an issue
- **License:** Apache-2.0

This is a field-built utility, not an officially supported Snowflake product.
