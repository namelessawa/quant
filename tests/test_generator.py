"""Candidate generation, kept strictly separate from submission."""

from __future__ import annotations

import csv

import pytest

from worldquant.generator import AlphaGenerator, CombinatorialGenerator, StaticGenerator


class TestCombinatorialGenerator:
    def test_cross_product(self):
        gen = CombinatorialGenerator(
            operators=["ts_mean", "ts_std_dev"], fields=["close"], windows=[5, 10]
        )
        assert gen.generate() == [
            "rank(ts_mean(close, 5))",
            "rank(ts_mean(close, 10))",
            "rank(ts_std_dev(close, 5))",
            "rank(ts_std_dev(close, 10))",
        ]

    def test_matches_the_documented_example(self):
        gen = CombinatorialGenerator(
            operators=["ts_delta"], fields=["close"], windows=[5, 10, 20]
        )
        assert gen.generate() == [
            "rank(ts_delta(close, 5))",
            "rank(ts_delta(close, 10))",
            "rank(ts_delta(close, 20))",
        ]

    def test_defaults_are_the_documented_lists(self):
        expressions = CombinatorialGenerator().generate()
        assert len(expressions) == 3 * 3 * 4
        assert "rank(ts_mean(close, 5))" in expressions
        assert "rank(ts_std_dev(volume, 60))" in expressions
        assert "rank(ts_delta(returns, 20))" in expressions

    def test_max_count_caps_output(self):
        gen = CombinatorialGenerator(max_count=3)
        assert len(gen.generate()) == 3

    def test_max_count_larger_than_the_product(self):
        gen = CombinatorialGenerator(
            operators=["ts_mean"], fields=["close"], windows=[5], max_count=99
        )
        assert len(gen.generate()) == 1

    def test_duplicates_are_removed(self):
        gen = CombinatorialGenerator(
            operators=["ts_mean", "ts_mean"], fields=["close"], windows=[5]
        )
        assert gen.generate() == ["rank(ts_mean(close, 5))"]

    def test_custom_wrapper_and_template(self):
        gen = CombinatorialGenerator(
            operators=["ts_delta"], fields=["close"], windows=[5],
            wrapper="zscore", template="-{wrapper}({operator}({field}, {window}))",
        )
        assert gen.generate() == ["-zscore(ts_delta(close, 5))"]

    def test_empty_operator_list(self):
        assert CombinatorialGenerator(operators=[]).generate() == []


class TestStaticGenerator:
    def test_returns_the_given_expressions(self):
        assert StaticGenerator(["rank(close)"]).generate() == ["rank(close)"]

    def test_strips_and_skips_blanks(self):
        gen = StaticGenerator(["  rank(close)  ", "", "   "])
        assert gen.generate() == ["rank(close)"]

    def test_is_independent_of_the_input_list(self):
        source = ["rank(close)"]
        gen = StaticGenerator(source)
        gen.generate().append("mutated")
        assert gen.generate() == ["rank(close)"]


class TestGeneratorContract:
    def test_subclasses_must_implement_generate(self):
        class Incomplete(AlphaGenerator):
            pass

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_generate_does_not_touch_the_network(self):
        # A generator has no client and no session by construction; this asserts
        # the separation the spec asks for.
        gen = CombinatorialGenerator()
        assert not hasattr(gen, "client")
        assert not hasattr(gen, "session")
        assert len(gen.generate()) == 36

    def test_to_specs_produces_runnable_specs(self):
        specs = CombinatorialGenerator(
            operators=["ts_mean"], fields=["close"], windows=[5, 10]
        ).to_specs(settings={"region": "USA"})
        assert len(specs) == 2
        assert specs[0].name.startswith("alpha_")
        assert specs[0].settings["region"] == "USA"


class TestWritingCandidates:
    def test_writes_csv_with_name_and_expression(self, tmp_path):
        out = tmp_path / "candidates.csv"
        CombinatorialGenerator(
            operators=["ts_delta"], fields=["close"], windows=[5, 10]
        ).write(out)

        with out.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        names = [row["name"] for row in rows]
        assert all(name.startswith("alpha_") for name in names)
        assert len(set(names)) == len(names)
        assert [row["expression"] for row in rows] == [
            "rank(ts_delta(close, 5))", "rank(ts_delta(close, 10))",
        ]

    def test_written_csv_can_be_loaded_back(self, tmp_path):
        from worldquant.loader import load_alphas

        out = tmp_path / "candidates.csv"
        CombinatorialGenerator(
            operators=["ts_mean"], fields=["volume"], windows=[20]
        ).write(out)
        specs = load_alphas(out, default_settings={"region": "USA"})
        assert len(specs) == 1
        assert specs[0].expression == "rank(ts_mean(volume, 20))"

    def test_writes_plain_text_for_other_extensions(self, tmp_path):
        out = tmp_path / "candidates.txt"
        CombinatorialGenerator(
            operators=["ts_delta"], fields=["close"], windows=[5]
        ).write(out)
        assert out.read_text(encoding="utf-8").strip() == "rank(ts_delta(close, 5))"

    def test_creates_missing_parent_directories(self, tmp_path):
        out = tmp_path / "nested" / "dir" / "candidates.txt"
        CombinatorialGenerator(
            operators=["ts_delta"], fields=["close"], windows=[5]
        ).write(out)
        assert out.exists()

    def test_write_returns_the_path(self, tmp_path):
        out = tmp_path / "candidates.txt"
        returned = CombinatorialGenerator(
            operators=["ts_delta"], fields=["close"], windows=[5]
        ).write(out)
        assert returned == out
