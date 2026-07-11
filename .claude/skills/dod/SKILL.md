---
name: dod
description: Run this repo's Definition of Done (pytest, ruff, mypy, lint-imports) and report the verdict. Use before declaring any change complete, before commits, or when asked to "run the checks" / "run the tests".
allowed-tools: Bash(python -m pytest *) Bash(pytest *) Bash(ruff *) Bash(mypy *) Bash(lint-imports*)
---

Run the four Definition of Done checks (CLAUDE.md; CI runs exactly these):

```bash
python -m pytest -q
ruff check src tests evals
mypy src
lint-imports
```

Run all four even if an early one fails — the point is the complete
picture. Then report a one-line verdict per check (pass/fail, counts),
leading with the overall result.

On failures: quote the actual failing output (test names + assertion, lint
rule + location), not a paraphrase. Do not auto-fix anything as part of
this skill — fixing is the caller's decision. The only exception: if
`ruff check` reports fixable import-order errors (I001) in files just
written this session, run `ruff check --fix` on those paths and re-check.

Live E2E (`ANYTYPE_E2E=1 pytest tests/e2e -q`) is NOT part of DoD; mention
it only if the change touched `infrastructure/anytype/` and suggest the
user may want a live pass too.
