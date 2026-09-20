# Contributing to Blessing AI

This repository moves real money on Binance. The contribution rules below
exist to keep the fail-closed guarantees intact.

## Ground rules

1. **Never** commit or paste API keys, API secrets, passwords, DSNs, Firebase
   tokens, OIDC tokens, or listen keys. Secrets live in Google Secret Manager
   and are injected only into the Worker (see
   `docs/MAINNET-RELEASE-RUNBOOK.md`).
2. Research/AI diagnostics never get live order authority; do not add a code
   path where they could.
3. Changes to the release gate, risk caps, or order execution must update the
   gate tests and `docs/MAINNET-RELEASE-RUNBOOK.md` in the same PR.
4. The Worker must remain fail-closed: missing/stale evidence disarms it.

## Workflow

- Branches: `codex/<topic>` (trunk is `main`).
- Commits: [Conventional Commits](https://www.conventionalcommits.org/) —
  `feat(scope):`, `fix(scope):`, `test(scope):`, `docs:`, `chore(scope):`.
- PRs: fill the risk-class section of the PR template. `high`-risk PRs need
  evidence attached.

## Setup

```bash
pip install -e ".[dev]"
npm ci

# Optional but recommended: local hook gate
pip install pre-commit && pre-commit install
```

## Before you push

```bash
# Node (type check + ESLint; unused-vars is warn-level until the backlog is ratcheted)
npm run lint
npm test              # vitest

# Python (never runs credentialed Testnet suites locally)
pytest tests/python/ -m "not contract_readonly and not contract_mutating and not contract_soak"

# Python lint gate used by CI (error-level rules)
ruff check --select E9,F63,F7,F82 .
```

The repo also carries a broader aspirational ruff backlog (unused imports,
PEP 604/585 annotations, blind excepts) that is **not** a CI gate yet — do
not make it worse in new code, and do not bulk-fix it inside unrelated PRs.

## CI

GitHub Actions runs on every PR to `main`: Data Connect SDK drift check,
`tsc`, vitest, build, Python 3.13 unit suite with a coverage floor, the
error-level ruff gate, and a hygiene check against tracked cache/patch
artifacts. Credentialed Testnet contract/soak suites are manual-only via
`workflow_dispatch` (see `.github/workflows/testnet-contract.yml`).

## Reporting vulnerabilities

See [SECURITY.md](SECURITY.md) — private disclosure only.
