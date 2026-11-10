"""Offline regression tests (no Snowflake needed):  PYTHONPATH=scripts python3 -m unittest discover -s tests"""
import tempfile
import unittest
from pathlib import Path

from a2s.emit import Project
from a2s.expr import Unsupported, eval_py, qid, to_sql
from a2s.parse import inventory, parse_workflow

FIX = Path(__file__).parent / "fixtures" / "mini.yxmd"


def sql(expr, kinds):
    return to_sql(expr, lambda n: (qid(n), kinds[n.lower()]))[0]


class ExprTests(unittest.TestCase):
    def test_round_to_multiple(self):
        self.assertEqual(sql("Round([x], 2)", {"x": "num"}), '(ROUND("x" / 2) * 2)')
        self.assertEqual(sql("Round([x], .01)", {"x": "num"}), 'ROUND("x", 2)')

    def test_string_equality_is_null_safe_and_case_insensitive(self):
        self.assertEqual(sql('[a] = "x"', {"a": "str"}), "EQUAL_NULL(UPPER(\"a\"), UPPER('x'))")

    def test_number_equality_has_no_upper(self):
        self.assertEqual(sql("[a] = 1", {"a": "num"}), 'EQUAL_NULL("a", 1)')

    def test_string_concat_null_semantics(self):
        self.assertIn("COALESCE", sql('[a] + "/"', {"a": "str"}))
        self.assertEqual(sql("[a] + [b]", {"a": "num", "b": "num"}), '("a" + "b")')

    def test_if_elseif(self):
        s = sql('IF [a] = "x" THEN 1 ELSEIF [a] = "y" THEN 2 ELSE 3 ENDIF', {"a": "str"})
        self.assertTrue(s.startswith("CASE WHEN") and s.endswith("ELSE 3 END"))

    def test_unknown_function_raises(self):
        with self.assertRaises(Unsupported):
            sql("Spatial_Area([a])", {"a": "str"})

    def test_dynamic_rename_eval(self):
        self.assertEqual(eval_py('Right([_CurrentField_], 2)+"/"+Left([_CurrentField_],4)',
                                 {"_CurrentField_": "2025_07"}), "07/2025")


class ConvertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wf = parse_workflow(FIX)
        cls.proj = Project(cls.wf, "fx", {}).build()
        cls.rep = cls.proj.report()

    def test_inventory(self):
        inv = inventory(self.wf)
        self.assertEqual(inv["tool_count"], 16)
        self.assertEqual(inv["unsupported_tools"], [])

    def test_no_todos(self):
        self.assertEqual(self.rep["counts"]["TODO"], 0, self.rep["issues"])

    def test_join_right_prefix_only_on_collision(self):
        names = [f.name for f in self.proj.conv.out[("6", "Join")].fields]
        self.assertIn("Right_Customer", names)
        self.assertIn("Region", names)

    def test_formula_chain_and_replace(self):
        body = "\n".join(s for c, s in self.proj.conv.ctes.items() if c.startswith("t4_"))
        self.assertIn('* REPLACE (TRIM("Customer") AS "Customer")', body)
        self.assertGreaterEqual(sum(1 for c in self.proj.conv.ctes if c.startswith("t4_")), 2)

    def test_unique_uses_upstream_sort(self):
        self.assertIn('ORDER BY "Amount" DESC NULLS LAST', self.proj.conv.ctes["t10_unique_rn"])

    def test_crosstab_transpose_rename(self):
        names = [f.name for f in self.proj.conv.out[("12", "Output")].fields]
        self.assertEqual(names, ["Customer", "01/2025", "02/2025"])
        self.assertEqual(self.proj.conv.ctes[self.proj.conv.out[("13", "Output")].cte].count("UNION ALL"), 1)

    def test_cleanse(self):
        s = self.proj.conv.ctes[self.proj.conv.out[("14", "Output")].cte]
        self.assertIn("UPPER(TRIM(COALESCE(\"Region\", '')", s)

    def test_project_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.proj.write(d)
            for f in ("dbt_project.yml", "profiles.yml", "models/sources.yml", "models/marts/fx__out_summary.sql",
                      "seeds/fx__seed_t2.csv", "tests/fx__test_t15_1.sql", "parity/fx__out_summary.sql"):
                self.assertTrue((root / f).exists(), f)
            self.assertNotIn("{% raw %}{{", (root / "models/marts/fx__out_summary.sql").read_text())


if __name__ == "__main__":
    unittest.main()
