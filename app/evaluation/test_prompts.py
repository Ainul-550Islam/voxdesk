"""Unit tests for app.evaluation.prompts.

Co-located with the package (not under tests/) for the same reason as the
analytics tests: the repo conftest needs the SQLAlchemy/async stack. Run with:
python -m pytest app/evaluation/ -q
"""

from __future__ import annotations

import pytest

from app.evaluation.prompts import PromptTemplate, render_prompt


def test_render_basic_and_repeated_placeholders():
    t = PromptTemplate("Hi {name}, this is {business} — {name} speaking.")
    assert t.render(name="Ada", business="Acme") == "Hi Ada, this is Acme — Ada speaking."
    assert t.placeholders() == frozenset({"name", "business"})


def test_missing_value_raises_with_name():
    t = PromptTemplate("hello {name}")
    with pytest.raises(KeyError, match="name"):
        t.render()


def test_extra_values_are_ignored():
    t = PromptTemplate("hello {name}")
    assert t.render(name="Ada", bonus="ignored", other=1) == "hello Ada"


def test_values_with_braces_are_inserted_verbatim():
    # The core safety property: a brace in a value is a character, not syntax.
    t = PromptTemplate("Today: {fact}")
    rendered = t.render(fact="closes at 5pm {sharp} and costs {0:.2f}")
    assert rendered == "Today: closes at 5pm {sharp} and costs {0:.2f}"


def test_escaped_braces_render_literally():
    t = PromptTemplate("{{literal}} and {name}")
    assert t.render(name="x") == "{literal} and x"
    assert t.placeholders() == frozenset({"name"})


def test_non_string_values_are_coerced():
    assert render_prompt("n={n}", n=5) == "n=5"
    assert render_prompt("t={t}", t=True) == "t=True"
    assert render_prompt("n={n}", n=None) == "n=None"


def test_empty_template():
    t = PromptTemplate("")
    assert t.render() == ""
    assert t.placeholders() == frozenset()


def test_invalid_placeholders_rejected_at_construction():
    with pytest.raises(ValueError):
        PromptTemplate("{1abc}")  # not an identifier
    with pytest.raises(ValueError):
        PromptTemplate("{name!r}")  # conversions are not supported
    with pytest.raises(ValueError):
        PromptTemplate("{name:>10}")  # format specs are not supported
    with pytest.raises(ValueError):
        PromptTemplate("{name")  # unbalanced open brace
    with pytest.raises(ValueError):
        PromptTemplate("stray } brace")  # unbalanced close brace


def test_underscore_and_digit_identifiers_are_ok():
    t = PromptTemplate("{slot_minutes} at {t2}")
    assert t.render(slot_minutes=30, t2="noon") == "30 at noon"


def test_adjacent_escape_and_placeholder():
    t = PromptTemplate("{{{name}}}")  # "{" + placeholder + "}"
    assert t.render(name="Ada") == "{Ada}"


def test_render_prompt_convenience():
    assert render_prompt("hi {name}", name="Ada") == "hi Ada"
