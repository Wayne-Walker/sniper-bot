# sniper-bot — working rules

## Changelog is mandatory

**Every change to code in this repo must be recorded in `CHANGELOG.md` at the
repo root.** This applies to any session that touches the code — treat it as part
of "done", not an optional extra.

- Add the entry under `## [Unreleased]`, in `### Added` / `### Changed` /
  `### Fixed`, **newest first**.
- Each entry: a one/two-line summary, an **absolute date** (YYYY-MM-DD, convert
  any relative date), and the **file(s)** touched in trailing parentheses.
- Keep it concise but specific — enough that a future reader knows what changed
  and why, without reading the diff.
- If a change spans this repo and the multi-bot, log the sniper-bot side here and
  the multi-bot side in `multi-bot/CHANGELOG.md` (that repo has its own rules).

This mirrors the multi-bot's changelog discipline so the whole system has a
continuous, self-documenting history.

## Notes
- This project is Python (pm2-run scripts); it is **not** a git repository, so the
  changelog is the primary change record — there is no commit history to fall back
  on. Keep it current.
