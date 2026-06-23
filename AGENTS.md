# AGENTS.md

Guidance for Codex (and any AI agent) working in this repo. These rules are
**mandatory** and override default behavior. The same rules live in
[CLAUDE.md](CLAUDE.md) for Claude Code.

## Project

**Whenever** — a Flask app (`app.py` + `templates/index.html`) that finds best-value
flexible-date flights. Real fares come from flight APIs; a local DeepSeek model (via
Ollama) only transforms/analyzes that data — **it never originates prices**. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

- Language: Python 3.10+
- Tests: `pytest` + `pytest-cov` (unit), Playwright (e2e)
- CI: GitHub Actions

## Workflow rules

1. **CI gates merge to `main`.** No change reaches `main` unless required CI checks pass.
   `main` is protected — land work through a branch + PR, never by pushing directly.
2. **Merge as soon as CI is green.** When all required checks pass on a PR, merge it —
   don't let green branches sit.
3. **Run a code review before every push/commit.** Run the review on your diff and resolve
   its findings *before* you commit and push. No push without a clean review.
4. **Update tests and docs with every change.** Any behavior change must ship with updated
   tests and updated docs (`README.md`, `CONTRIBUTING.md`, `docs/*`) in the same change.
5. **Always write unit *and* e2e tests; keep coverage ≥ 99%.** Every feature/bugfix gets
   both unit tests and an end-to-end test. CI fails the build if line coverage drops below
   99% (`pytest --cov --cov-fail-under=99`).

## Definition of done

A change is done only when **all** are true:

- [ ] Unit + e2e tests written/updated and passing locally
- [ ] Coverage ≥ 99% (`pytest --cov=app --cov-fail-under=99`)
- [ ] Docs updated for the change
- [ ] Code review run and findings resolved
- [ ] Pushed to a branch, PR opened, CI green → merged to `main`

## Commands

```bash
pytest --cov=app --cov-fail-under=99 --cov-report=term-missing   # unit + coverage gate
pytest tests/e2e                                                  # Playwright e2e
python3 app.py                                                    # run locally
```

## Project-specific guardrails

- **Prices are real-data only.** The LLM may transform/analyze fares, never invent them.
  Any pricing path must hit a real flight API and normalize to the `get_fare` dict shape.
- Keep flight providers behind the `get_fare()` adapter.
- Never commit secrets — use `.env` (git-ignored); see `.env.example`.
