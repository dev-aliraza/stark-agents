"""The triggerRule expression language."""

from __future__ import annotations

import pytest

from stark.triggers import TriggerRuleError, parse

MESSAGE = {
    "text": "===== ArgoCD is down =====",
    "user": "U123",
    "channel": "C0SUPPORT",
    "thread": "1700000000.0001",
}


def matches(expression: str, values: dict | None = None) -> bool:
    return parse(expression).matches(MESSAGE if values is None else values)


# --- single conditions ----------------------------------------------------------------


def test_contains():
    assert matches('text.contains("ArgoCD")') is True
    assert matches('text.contains("Jenkins")') is False


def test_not_contains():
    assert matches('text.notContains("Jenkins")') is True
    assert matches('text.notContains("ArgoCD")') is False


def test_matching_is_case_insensitive():
    assert matches('text.contains("argocd")') is True
    assert matches('text.contains("ARGOCD")') is True


def test_operator_and_field_names_are_case_insensitive():
    assert matches('TEXT.CONTAINS("ArgoCD")') is True
    assert matches('text.NOTCONTAINS("Jenkins")') is True


def test_every_documented_field_is_readable():
    assert matches('user.contains("U123")') is True
    assert matches('channel.contains("SUPPORT")') is True
    assert matches('thread.contains("1700000000")') is True


def test_the_five_equals_trigger_from_the_spec():
    assert matches('text.contains("=====")') is True


# --- combinators ---------------------------------------------------------------------


def test_and():
    assert matches('text.contains("ArgoCD") and text.contains("down")') is True
    assert matches('text.contains("ArgoCD") and text.contains("Jenkins")') is False


def test_or():
    assert matches('text.contains("Jenkins") or text.contains("ArgoCD")') is True
    assert matches('text.contains("Jenkins") or text.contains("Nomad")') is False


def test_not():
    assert matches('not text.contains("Jenkins")') is True
    assert matches('not text.contains("ArgoCD")') is False


def test_and_binds_tighter_than_or():
    # Parsed as: A or (B and C). With A true the whole thing is true regardless of C.
    assert matches(
        'text.contains("ArgoCD") or text.contains("Jenkins") and text.contains("Nomad")'
    ) is True
    # Parsed as: (A and B) or C — both branches false.
    assert matches(
        'text.contains("ArgoCD") and text.contains("Jenkins") or text.contains("Nomad")'
    ) is False


def test_parentheses_override_precedence():
    assert matches(
        '(text.contains("ArgoCD") or text.contains("Jenkins")) and text.contains("down")'
    ) is True
    assert matches(
        '(text.contains("ArgoCD") or text.contains("Jenkins")) and text.contains("Nomad")'
    ) is False


def test_not_applies_to_a_parenthesised_group():
    assert matches('not (text.contains("Jenkins") or text.contains("Nomad"))') is True
    assert matches('not (text.contains("Jenkins") or text.contains("ArgoCD"))') is False


def test_the_users_worked_example():
    rule = (
        '(text.contains("ABC") and text.contains("XYZ")) '
        'and channel.notContains("PODUEMCJE")'
    )
    assert parse(rule).matches(
        {"text": "ABC and XYZ", "channel": "C0SUPPORT", "user": None, "thread": None}
    ) is True
    # Excluded channel.
    assert parse(rule).matches(
        {"text": "ABC and XYZ", "channel": "PODUEMCJE-1", "user": None, "thread": None}
    ) is False
    # Missing one required term.
    assert parse(rule).matches(
        {"text": "ABC only", "channel": "C0SUPPORT", "user": None, "thread": None}
    ) is False


def test_original_nested_example_with_or_of_two_ands():
    rule = (
        '(text.contains("ABC") and text.contains("XYZ")) '
        'or (text.contains("GFH") and text.notContains("OPI"))'
    )
    parsed = parse(rule)

    def check(text: str) -> bool:
        return parsed.matches({"text": text, "user": None, "channel": None, "thread": None})

    assert check("ABC XYZ") is True
    assert check("GFH alone") is True
    assert check("GFH with OPI") is False
    assert check("ABC only") is False


def test_deep_nesting():
    rule = 'not (not (text.contains("ArgoCD") and (text.contains("down") or text.contains("up"))))'
    assert matches(rule) is True


# --- absent fields -------------------------------------------------------------------


EMPTY = {"text": "hello", "user": None, "channel": None, "thread": None}


def test_absent_field_cannot_contain_anything():
    assert parse('channel.contains("SUPPORT")').matches(EMPTY) is False


def test_absent_field_passes_notcontains_vacuously():
    """This is why a channel guard is a no-op under the CLI listener."""
    assert parse('channel.notContains("PODUEMCJE")').matches(EMPTY) is True


def test_a_channel_guard_still_fires_off_slack():
    rule = 'text.contains("=====") and channel.notContains("PODUEMCJE")'
    assert parse(rule).matches(
        {"text": "===== x =====", "user": "cli", "channel": None, "thread": None}
    ) is True


def test_missing_key_is_treated_as_absent():
    assert parse('channel.contains("X")').matches({"text": "hi"}) is False
    assert parse('channel.notContains("X")').matches({"text": "hi"}) is True


def test_empty_string_field_is_not_none():
    assert parse('channel.contains("X")').matches({"channel": ""}) is False
    assert parse('channel.notContains("X")').matches({"channel": ""}) is True


# --- literals ------------------------------------------------------------------------


def test_literal_may_contain_spaces_and_punctuation():
    assert parse('text.contains("is down")').matches(MESSAGE) is True
    assert parse('text.contains("=====")').matches(MESSAGE) is True


def test_escaped_quote_inside_a_literal():
    rule = r'text.contains("say \"hi\"")'
    assert parse(rule).matches({"text": 'they say "hi" often'}) is True


def test_escaped_backslash_inside_a_literal():
    rule = r'text.contains("a\\b")'
    assert parse(rule).matches({"text": r"a\b"}) is True


def test_parens_inside_a_literal_do_not_confuse_the_parser():
    assert parse('text.contains("(ABC)")').matches({"text": "x (ABC) y"}) is True


def test_keywords_inside_a_literal_are_not_operators():
    assert parse('text.contains("and or not")').matches({"text": "x and or not y"}) is True


# --- introspection -------------------------------------------------------------------


def test_source_is_preserved_for_logging():
    rule = parse('  text.contains("A")  ')
    assert rule.source == 'text.contains("A")'
    assert str(rule) == 'text.contains("A")'


def test_fields_reports_what_the_rule_reads():
    rule = parse('text.contains("A") and channel.notContains("B") or user.contains("C")')
    assert rule.fields() == {"text", "channel", "user"}


# --- errors, all raised at parse time ------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ('text.contains("A"', r"expected '\)'"),
        ('text.contains "A")', r"expected '\('"),
        ("text.contains()", "quoted literal"),
        ('text.contains("")', "empty literal"),
        ('text.startsWith("A")', "unknown operator"),
        ('message.contains("A")', "unknown field"),
        ('text.contains("A") and', "field name"),
        ('and text.contains("A")', "field name"),
        ('text.contains("A") text.contains("B")', "missing 'and' or 'or'"),
        ('(text.contains("A")', r"expected '\)'"),
        ('text.contains("A"))', "unexpected"),
        ('text contains("A")', "expected '.'"),
        ('text.contains("unterminated)', "unterminated string"),
        ('text.contains(A)', "quoted literal"),
        ("text.contains($)", "unexpected character"),
        ("not", "field name"),
    ],
)
def test_malformed_expressions_are_rejected(expression, expected):
    with pytest.raises(TriggerRuleError, match=expected):
        parse(expression)


def test_error_messages_carry_a_position():
    with pytest.raises(TriggerRuleError, match="position"):
        parse('text.contains("A") and badfield.contains("B")')


def test_non_string_rule_is_rejected_with_a_quoting_hint():
    with pytest.raises(TriggerRuleError, match="must be a string"):
        parse(True)  # YAML turns an unquoted `yes` into a bool
