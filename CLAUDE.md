# StemSplitter

Desktop app (Windows/macOS) that splits songs into stems with RoFormer models. The full technical reference is `docs/DOCUMENTATION.md`; the quick start for users is `README.md`.

## Rule: everything is written in English

All code, documentation, commit messages and pull requests are written in **English**: identifiers, comments, docstrings, log and UI strings, Markdown files, commit subjects and bodies, PR titles and descriptions, and review comments.

- Talking with the user in another language is fine. This rule covers what goes into the repository and onto GitHub.

## Rule: every relevant change must be documented

A relevant change is not done until the documentation describes the new state of the project. The documentation update goes **in the same commit or PR** as the change, never "later".

**Relevant:** any change to behavior, features, UI or CLI options, presets, models, output formats, architecture (processes, threads, the worker protocol), performance or memory, dependencies or requirements, packaging, CI or releases, known limitations, and any bug fix the user can notice.

**Not relevant:** refactors with no visible effect, comment tweaks and typo fixes.

**What to update:**
- `docs/DOCUMENTATION.md`: the affected section (features, quality, performance, architecture, CI, limitations), and **always** a new row in the section 9 table (change history). A commit cannot quote its own hash: put `—` in the row and fill in the hash the next time the documentation is updated.
- `README.md`: whenever the change affects people who download, install or use the app (features, timings, requirements, options, limitations, file layout).
- The docstrings and comments that describe the changed behavior.
- Remove or fix whatever is no longer true. The documentation describes the **current** state and does not keep old versions, except in the history.

**Numbers and claims:**
- Every quality, timing or memory number must come from a real measurement, with the hardware and conditions stated.
- If a number is out of date and has not been measured again, say so explicitly in the text. Do not estimate or invent a new value.
- For optimizations, record whether the output is still bit-identical, or how much it changed.

**Before finishing:**
- Reread the sections you touched and check they don't contradict the code or other sections.
- Run the CI lint: `ruff check .` and `python -m compileall -q main.py stemsplitter StemSplitter.spec`.
