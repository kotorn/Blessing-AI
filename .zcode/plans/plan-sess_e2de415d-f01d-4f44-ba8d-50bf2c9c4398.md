# Resume Web GUI Test — Blessing AI control plane (http://localhost:3000)

## Why restart from scratch
The interrupted session persisted no artifacts (no screenshots, reports, or notes anywhere in the repo or `C:\Users\Kan\.zcode`), so there is nothing to literally resume. I will re-run the test per the web-gui-tester skill, with evidence capture this time.

## Defaults applied (no user answers returned)
- **Scope**: full sweep (P0→P3) across cockpit, wallet, backtest, copilot, bigquery, architecture tabs.
- **Mutating controls** (kill-switch, arm/continue, trading wizard): **observe only** — verify rendering, dialogs, gating; never confirm a state-changing action.
- **Auth**: drive the GUI to the Sign In popup; if you complete the Google sign-in, authenticated test points run; otherwise they're recorded as blocked-pending-credentials. Unauthenticated shell/401 handling is fully testable either way.

## Phase A — Environment preparation
1. Start the web server in the background: `npm run dev` in `H:\Blessing AI`, wait until `http://localhost:3000` and `/api/health` respond. **Do not** start docker/worker/trading stack — avoids any trading side effects; worker-offline degradation is itself a valid test subject.
2. Create evidence folder `H:\Blessing AI\gui-test-screenshots\` and copy each browser screenshot artifact there as `tN_<step>.png`.
3. Bootstrap browser automation via `mcp__node_repl__js` (control-browser skill: `setupBrowserRuntime`, backend via `getForUrl("http://localhost:3000/")`, read full API guide).
4. State: "Environment preparation is complete; formal testing is beginning."

## Phase B — Test execution (black-box GUI only)
| # | Pri | Test point |
|---|-----|-----------|
| T1 | P0 | Initial load: shell renders, header/nav present, no blank screen or console-visible errors |
| T2 | P0 | Tab navigation: each of the 6 tabs renders real content (screenshot each) |
| T3 | P0 | Sign-in flow: Sign In button → Google popup appears (screenshot); proceed authenticated if completed |
| T4 | P1 | Unauthenticated 401 handling: data views show graceful error/empty states, no crash |
| T5 | P1 | Interaction feedback: loading states, disabled buttons, error banners, toast/dialog behavior |
| T6 | P1 | Dangerous controls (observe-only): kill-switch, arm/continue, StartTradingWizard — presence, confirmation dialogs, gating/disabled states |
| T7 | P2 | Input boundaries on client-side forms (backtest params, copilot input): empty, long, special characters |
| T8 | P3 | Layout & styling: overlap, truncation/overflow, alignment, contrast across tabs |

Method per skill: locate elements only from live DOM snapshots (never guessed selectors); one state-changing action per observation cycle; `domSnapshot()` + viewed screenshot cross-validation at every state change; no JS injection, no URL construction, no force clicks, no refresh-to-escape; transient states captured before/action/wait/after in one call. Console log listening only if the tooling supports it read-only; otherwise visible error manifestations + screenshots.

## Phase C — Deliverable
A Markdown test report (in the final message, screenshots interleaved via `file:///` paths from `gui-test-screenshots/`): per-test-point pass/fail with evidence, reproduction steps for failures, blocked points (e.g., auth), all environment-prep actions disclosed, and page error manifestations observed.

## Verification
- Server health checked before testing; every test point backed by at least one viewed screenshot; report only claims corroborated by both DOM state and screenshot.