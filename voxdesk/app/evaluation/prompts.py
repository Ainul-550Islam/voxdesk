"""Safe prompt templating (Phase 4, evaluation slice).

``PromptTemplate`` is the safe replacement for building prompts with
``str.format``. The difference is exactly one property, and it is the one
that matters for prompt-injection resistance:

* ``str.format`` re-parses *values* for brace syntax, so a tenant fact like
  ``"closes at 5pm {sharp}"`` raises ``ValueError`` — and, worse, a value that
  happens to look like a format spec gets interpreted, not printed.
* ``PromptTemplate`` parses the template **once, up front**, and inserts values
  **verbatim**. A brace inside a value is just a character; it can never be
  re-read as a placeholder, a conversion (``!r``) or a format spec (``:>10``).

Placeholders use ``{name}`` with plain identifiers only; ``{{`` and ``}}``
escape literal braces. Conversions and format specs are rejected at
construction time, so there is no format-string surface to exploit.
"""

from __future__ import annotations

import re

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _scan(template: str) -> set[str]:
    """Find every placeholder and reject anything else — a single pass that
    runs at construction time so ``render`` can trust the structure."""
    placeholders: set[str] = set()
    i = 0
    n = len(template)
    while i < n:
        ch = template[i]
        if ch == "{":
            if i + 1 < n and template[i + 1] == "{":
                i += 2  # escaped literal brace
                continue
            j = i + 1
            while j < n and template[j] not in "{}":
                j += 1
            if j >= n or template[j] != "}":
                raise ValueError("unbalanced '{{' in prompt template")
            name = template[i + 1 : j]
            if not _IDENT_RE.match(name):
                raise ValueError(f"invalid placeholder {name!r} in prompt template")
            placeholders.add(name)
            i = j + 1
        elif ch == "}":
            if i + 1 < n and template[i + 1] == "}":
                i += 2  # escaped literal brace
                continue
            raise ValueError("unbalanced '}}' in prompt template")
        else:
            i += 1
    return placeholders


class PromptTemplate:
    """A validated, injection-safe prompt template.

    Construct once, render many times. ``render`` raises ``KeyError`` (naming
    the placeholder) when a required value is missing; unknown extra keyword
    arguments are ignored, so a caller may pass a shared dict of values.
    """

    def __init__(self, template: str):
        self._template = template
        self._placeholders = frozenset(_scan(template))

    @property
    def template(self) -> str:
        return self._template

    def placeholders(self) -> frozenset[str]:
        """The placeholder names the template requires (``frozenset``)."""
        return self._placeholders

    def render(self, **values: object) -> str:
        """Render with the given values. Values are stringified and inserted
        verbatim — never re-parsed."""
        out: list[str] = []
        template = self._template
        i = 0
        n = len(template)
        while i < n:
            ch = template[i]
            if ch == "{":
                if i + 1 < n and template[i + 1] == "{":
                    out.append("{")
                    i += 2
                    continue
                j = i + 1
                while j < n and template[j] != "}":
                    j += 1
                name = template[i + 1 : j]
                if name not in values:
                    raise KeyError(f"missing value for placeholder {name!r}")
                out.append(str(values[name]))
                i = j + 1
            elif ch == "}":
                out.append("}")
                i += 2
            else:
                out.append(ch)
                i += 1
        return "".join(out)


def render_prompt(template: str, **values: object) -> str:
    """One-shot convenience: ``render_prompt("hi {name}", name="Ada")``."""
    return PromptTemplate(template).render(**values)
