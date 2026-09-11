"""Expression normalization and hashing."""

from __future__ import annotations

from worldquant.hashing import (
    SCOPE_SETTINGS,
    auto_alpha_id,
    dedup_key,
    expression_hash,
    normalize_expression,
    scope_hash,
)


class TestNormalizeExpression:
    def test_strips_and_collapses_whitespace(self):
        assert normalize_expression("  rank( ts_delta(close,  5) )  ") == "rank(ts_delta(close,5))"

    def test_equivalent_spacing_normalizes_identically(self):
        assert normalize_expression("rank(ts_delta(close, 5))") == normalize_expression(
            "rank( ts_delta(close,5) )"
        )

    def test_tabs_and_newlines_are_whitespace(self):
        assert normalize_expression("rank(\n\tclose ,\t5\n)") == "rank(close,5)"

    def test_removes_spaces_around_operators(self):
        assert normalize_expression("rank(close) - rank(volume)") == "rank(close)-rank(volume)"

    def test_unary_minus_keeps_token_intact(self):
        assert normalize_expression("- rank(ts_std_dev(returns, 20))") == "-rank(ts_std_dev(returns,20))"

    def test_keeps_separator_between_word_tokens(self):
        # Collapsing this to "ts_deltaclose" would invent a different identifier.
        assert normalize_expression("ts_delta close") == "ts_delta close"

    def test_preserves_string_literal_contents(self):
        normalized = normalize_expression("group_neutralize( x , 'some industry' )")
        assert normalized == "group_neutralize(x,'some industry')"

    def test_preserves_double_quoted_literal(self):
        assert normalize_expression('f( x , "a  b" )') == 'f(x,"a  b")'

    def test_case_is_preserved(self):
        # BRAIN identifiers are case sensitive; lowering would change meaning.
        assert normalize_expression("Rank(Close)") == "Rank(Close)"
        assert normalize_expression("Rank(Close)") != normalize_expression("rank(close)")

    def test_empty_and_none_like_inputs(self):
        assert normalize_expression("") == ""
        assert normalize_expression("   ") == ""

    def test_dotted_field_names_survive(self):
        assert normalize_expression("rank( analyst4.value )") == "rank(analyst4.value)"


class TestExpressionHash:
    def test_is_deterministic(self):
        assert expression_hash("rank(close)") == expression_hash("rank(close)")

    def test_ignores_meaningless_whitespace(self):
        assert expression_hash("rank( close )") == expression_hash("rank(close)")

    def test_differs_for_different_expressions(self):
        assert expression_hash("rank(close)") != expression_hash("rank(volume)")

    def test_is_sha256_hex(self):
        digest = expression_hash("rank(close)")
        assert len(digest) == 64
        assert all(char in "0123456789abcdef" for char in digest)


class TestDedupKey:
    def test_same_expression_same_settings(self):
        settings = {"region": "USA", "delay": 1}
        assert dedup_key("rank(close)", settings) == dedup_key("rank( close )", dict(settings))

    def test_settings_order_does_not_matter(self):
        assert dedup_key("rank(close)", {"region": "USA", "delay": 1}) == dedup_key(
            "rank(close)", {"delay": 1, "region": "USA"}
        )

    def test_snake_case_alias_matches_camel_case(self):
        assert dedup_key("rank(close)", {"unit_handling": "VERIFY"}) == dedup_key(
            "rank(close)", {"unitHandling": "VERIFY"}
        )

    def test_different_region_is_a_different_alpha(self):
        assert dedup_key("rank(close)", {"region": "USA"}) != dedup_key(
            "rank(close)", {"region": "CHN"}
        )

    def test_different_delay_is_a_different_alpha(self):
        assert dedup_key("rank(close)", {"delay": 1}) != dedup_key("rank(close)", {"delay": 0})

    def test_missing_settings_use_defaults(self):
        # None and {} must agree, otherwise resume would resubmit everything.
        assert dedup_key("rank(close)", None) == dedup_key("rank(close)", {})

    def test_unknown_settings_keys_are_preserved(self):
        # New BRAIN settings must keep working without a code change, and must
        # still distinguish one alpha from another.
        assert dedup_key("rank(close)", {"someFutureFlag": True}) != dedup_key(
            "rank(close)", {"someFutureFlag": False}
        )

    def test_value_type_is_part_of_the_key(self):
        # Settings are compared structurally: delay 1 and "1" are normalized to
        # the same int, so they must agree, while delay 1 and 2 must not.
        assert dedup_key("rank(close)", {"delay": 1}) == dedup_key("rank(close)", {"delay": "1"})
        assert dedup_key("rank(close)", {"delay": 1}) != dedup_key("rank(close)", {"delay": 2})


class TestScopeHash:
    """Expression plus the information set — narrower than dedup_key, wider than
    expression_hash."""

    def test_region_universe_and_delay_define_the_scope(self):
        base = {"region": "USA", "universe": "TOP3000", "delay": 1}
        for tweak in ({"region": "CHN"}, {"universe": "TOP1000"}, {"delay": 0}):
            assert scope_hash("rank(close)", base) != scope_hash("rank(close)", {**base, **tweak}), tweak

    def test_construction_settings_do_not(self):
        base = {"region": "USA", "universe": "TOP3000", "delay": 1}
        for tweak in ({"decay": 16}, {"truncation": 0.05}, {"neutralization": "NONE"},
                      {"nanHandling": "ON"}, {"pasteurization": "OFF"}, {"testPeriod": "P2Y"}):
            assert scope_hash("rank(close)", base) == scope_hash("rank(close)", {**base, **tweak}), tweak

    def test_expression_is_still_normalized(self):
        settings = {"region": "USA", "universe": "TOP3000", "delay": 1}
        assert scope_hash("rank(close)", settings) == scope_hash("rank( close )", settings)
        assert scope_hash("rank(close)", settings) != scope_hash("rank(volume)", settings)

    def test_missing_settings_use_the_same_defaults(self):
        # None and {} must agree, or every stored row would look unsimulated.
        assert scope_hash("rank(close)", None) == scope_hash("rank(close)", {})

    def test_snake_case_alias_matches_camel_case(self):
        assert scope_hash("rank(close)", {"nan_handling": "ON"}) == scope_hash(
            "rank(close)", {"nanHandling": "ON"}
        )

    def test_delay_string_and_int_agree(self):
        assert scope_hash("rank(close)", {"delay": 1}) == scope_hash("rank(close)", {"delay": "1"})

    def test_scope_values_cannot_collide_by_concatenation(self):
        # region/universe/delay are joined with a separator, so shifting a
        # boundary between two of them must not produce the same hash.
        left = {"region": "USA", "universe": "TOP300", "delay": 0}
        right = {"region": "USAT", "universe": "OP300", "delay": 0}
        assert scope_hash("rank(close)", left) != scope_hash("rank(close)", right)

    def test_it_is_sha256_hex(self):
        digest = scope_hash("rank(close)", {"region": "USA"})
        assert len(digest) == 64
        assert all(char in "0123456789abcdef" for char in digest)

    def test_the_scope_settings_are_the_documented_three(self):
        assert SCOPE_SETTINGS == ("region", "universe", "delay")


class TestAutoAlphaId:
    def test_is_stable_and_prefixed(self):
        first = auto_alpha_id("rank(close)")
        assert first == auto_alpha_id("rank(close)")
        assert first.startswith("alpha_")
        assert len(first) == len("alpha_") + 10

    def test_differs_per_expression(self):
        assert auto_alpha_id("rank(close)") != auto_alpha_id("rank(volume)")

    def test_differs_per_settings(self):
        assert auto_alpha_id("rank(close)", {"region": "USA"}) != auto_alpha_id(
            "rank(close)", {"region": "CHN"}
        )
