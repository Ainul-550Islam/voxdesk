"""Prompt & evaluation pipelines (Phase 4 slice 2).

Pure, deterministic, stdlib-only building blocks for the prompt/eval surface:

* ``prompts.PromptTemplate`` — a *safe* prompt template. Unlike ``str.format``
  (which the existing ``app/agent/prompts.py`` uses today), a value containing
  braces is inserted verbatim and can never be re-parsed as instructions, and
  every placeholder is validated up front.
* ``scoring`` — deterministic text metrics (exact/contains/forbid, token F1,
  edit-distance similarity, number extraction).
* ``evaluator`` — a pass/fail-per-named-case harness with an injected model
  function, so a suite runs against any stub or live model without network
  access of its own.

Nothing here imports the database or the async stack, so the modules run and
test in a bare checkout the same way ``app/analytics`` does.
"""
