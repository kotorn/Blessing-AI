<!--
Conventional Commits title: feat(scope): | fix(scope): | test(scope): | docs: | chore(scope):
Branch convention: codex/<topic> -> main
-->

## Summary

<!-- What does this PR change and why? One or two sentences. -->

## Risk class

<!-- Pick one. Anything touching money-moving paths needs the highest bar. -->

- [ ] `none` — docs, comments, CI config
- [ ] `low` — control plane UI/backend, scripts, tooling
- [ ] `medium` — worker engines, persistence, venue adapters (Testnet path)
- [ ] `high` — release gate, risk caps, order execution, Mainnet path

## Checklist

- [ ] `npm run lint` and `npm test` pass locally
- [ ] `pytest tests/python/ -m "not contract_readonly and not contract_mutating and not contract_soak"` passes locally
- [ ] New/changed behavior is covered by tests
- [ ] No secrets, tokens, DSNs, or evidence material in the diff
- [ ] If release-gate or policy behavior changed: `docs/MAINNET-RELEASE-RUNBOOK.md` and gate tests updated in the same PR

## Evidence (for `high` risk PRs)

<!-- Gate test output, preflight diffs, or verification read-backs. -->
