# ZCODE-COMPLETION-AUDIT — Blessing-AI v0.2 (Lane A: Audit/Architecture)

- วันที่ตรวจ: 2026-09-22
- Branch: `zcode/finish-blessing-v0.2` | HEAD: `49d540ce78a693caac2ffb72b28eb04f1bc0ccec` (ตรวจด้วย `git rev-parse HEAD`)
- อ้างอิง: `Plan(4).md` (§1 safety rules, §3 Definition of Done, §13 readiness list, §6 cost workstream, §17 DataConnect decision)
- Baseline gates จาก script ที่รันให้ (ไม่ได้รันซ้ำตามกฎ): `npm-ci=FAIL(1)→PASS(0)`, `dataconnect-drift=PASS(0)`, `lint=PASS(0)`, `vitest=PASS(0)`, `build=PASS(0)`, `pip-dev=PASS(0)`, `ruff-error=PASS(0)`, `pytest-cov=PASS(0)`, `pytest=471 passed`

---

## สรุปผู้บริหาร (5 บรรทัด)

1. **โครงสร้างโค้ด v0.2 ครบทุกซับซิสเต็มและปลอดภัยแบบ fail-closed** — ไม่พบ secret/patch file ถูก track, Mainnet ถูก gate ด้วย `MAINNET_LIVE_APPROVED` + execution lease + release-controller validation, และ CI บน `main` เขียว (run 35556513219 ณ commit เดียวกับ HEAD ของ branch นี้)
2. **Trading Worker แข็งแรงที่สุด** — persistence/outbox/lease/reconciliation/kill switch/restart-safety มีโค้ดจริงและ test ครอบ (pytest 471 passed จาก baseline)
3. **Frontend/Control Plane ผ่าน gate ทั้งหมด** (lint/vitest/build/drift) และ Start Trading Wizard มี preflight blockers ตาม Plan §13 แต่**ยังขาด evidence label แบบ STALE/UNAVAILABLE ที่โจทย์ §3.2 กำหนดให้แยกตาชั่ง**
4. **ช่องว่างใหญ่อยู่ที่ "เอกสาร/หลักฐาน ไม่ใช่โค้ด"** — ยังไม่มี `docs/ZCODE-BACKLOG.md`, `docs/ZCODE-STAGING-UAT.md`, `docs/COST-BASELINE-LIVE.md`, ADR ของ DataConnect deferral, และ tooling `infra/artifact-registry/` ตาม Plan §6.5 ยังไม่ถูกสร้าง
5. **สถานะรวม: PARTIAL ทุกซับซิสเต็ม (ไม่มีอะไร BROKEN/MISSING ทั้งระบบ)** — เส้นทางสู่ `READY_FOR_OPERATOR_APPROVAL — NOT ARMED` ติดที่งานเอกสาร + Staging UAT + Artifact retention เป็นหลัก; Mainnet ปัจจุบัน NOT ARMED (deploy default `EXECUTION_MODE=PAPER, MAINNET_LIVE_APPROVED=false`)

---

## ตารางรายซับซิสเต็ม

| # | Subsystem | State | Evidence (path:line / คำสั่งที่รัน) | Gaps ที่ต้องทำ |
|---|---|---|---|---|
| 1 | Repository Hygiene | **PARTIAL** | • `git ls-files \| wc -l` → 357 ไฟล์; `git ls-files \| grep -iE "\.(patch\|diff\|bak\|tmp\|orig\|rej\|pem\|key...)..."` → ไม่พบ (มีแค่ `.env.example`)<br>• `git grep -lIiE "(api[_-]?key\|api[_-]?secret)\s*[:=]\s*['\"][A-Za-z0-9]{20,}"` → ไม่พบ hard-coded key<br>• `.env` บนดิสก์แต่ถูก ignore: `git check-ignore -v .env` → `.gitignore:8:.env*`<br>• CI มี hygiene gate จับ patch/cache ที่ track ผิด: `.github/workflows/ci.yml:60-66`<br>• Working tree: `git status --porcelain` → ` M infra/monitoring/verify-release-monitoring.ps1` (แก้ parsing gcloud format=value เท่านั้น, `git diff` ยืนยัน) + `?? Plan(4).md` (ไฟล์ plan เอง, untracked) | • README.md:1 ยังเขียน "**Blessing AI v0.1**" → docs drift กับ v0.2 (Plan §20)<br>• เอกสาร ZCODE deliverables ยังไม่มี: `docs/ZCODE-BACKLOG.md`, `docs/ZCODE-FINAL-REPORT.md`, `docs/ZCODE-STAGING-UAT.md`, `docs/COST-BASELINE-LIVE.md` (ทุกไฟล์ `ls` แล้ว No such file)<br>• ค้าง 1 ไฟล์แก้ไม่ commit (monitoring script) — ต้องตัดสินใจ commit หรือ revert |
| 2 | TypeScript / Frontend / Control Plane | **PARTIAL** | • Baseline gates ผ่านทั้งหมด (npm-ci=PASS(0) รอบสุดท้าย, dataconnect-drift/lint/vitest/build = PASS(0)) — อ้าง baseline ที่ script รันให้<br>• Routes ครบ 12 เส้นตาม Plan §13: `src/app/Routes.tsx:78-180` (`/command`, `/markets`, `/strategies`, `/orders`, `/positions`, `/risk`, `/portfolio`, `/research/replay`, `/analytics`, `/system/connections`, `/system/audit`, `/settings`) + navigation ครบ `src/app/navigation.ts:14-117`<br>• Evidence labeling: `src/contracts/system.ts:1` (`SystemMode = RESEARCH\|BACKTEST\|PAPER\|TESTNET\|LIVE\|UNKNOWN`), badge แยกสี LIVE(rose)/TESTNET(amber)/PAPER(cyan) ที่ `src/app/StatusBar.tsx:76-84`, banner `ILLUSTRATIVE_ONLY` ใช้ใน 8 การ์ด research เช่น `src/components/StrategyAttributionCard.tsx:129`, `BasketManager.tsx:105`<br>• Start Trading Wizard: `src/components/StartTradingWizard.tsx` 3 ขั้น, step 3 รัน preflight, ปุ่ม ARM ถูก disabled เมื่อ `!preflightResult?.canArm` (`StartTradingWizard.tsx:271`); `evaluatePreflight` ครอบ Mode/Env/Instruments/Strategy/Risk/Worker/Adapter/Private Stream/Account/Reconciliation/Market/Kill Switch/Mainnet Approval/Credentials/Preflight ที่ `src/backend/system.ts:50-124`; LIVE บังคับ instrument เดียว ETHUSDC (`StartTradingWizard.tsx:93` สอดคล้อง worker `apps/trading_worker/main.py:979`)<br>• Alerts เข้าถึงได้ + มี count: bell พร้อมจุดแดงเมื่อ `activeAlertCount > 0` ที่ `src/app/StatusBar.tsx:228-241`, drawer + dismissed filtering ที่ `src/app/AppShell.tsx:253-259`<br>• DataConnect cutover ปิด default: `server.ts:87`, `src/dataconnect/client.ts:16`, และ release candidate บังคับ `VITE_DATA_CONNECT_CUTOVER === false` ที่ `src/backend/release.ts:358` | • ไม่พบ label **STALE/UNAVAILABLE** ใน UI: `grep -rn "STALE\|UNAVAILABLE" src/ --include=*.tsx,*.ts` (ตัด dataconnect-generated) เจอเฉพาะ `BIGQUERY_UNAVAILABLE`/`UNAVAILABLE` ใน backend (`src/backend/bigquery.ts:321`, `src/backend/portfolio-margin.ts:10`) และ badge "Offline" ของ cloud-sync (`StatusBar.tsx:253`) — §3.2 ยังไม่ครบ<br>• Preflight checklist ยังไม่มีข้อ **Authentication / Role / Persistence** แยกชัดเหมือน 11 หัวข้อใน Plan §13 (มีแต่ enforce ผ่าน auth middleware/worker state)<br>• สถานะ "routes render จริงบน browser" อ้างจาก vitest/build ผ่าน ไม่ได้ทดสอบ browser จริงในรอบนี้ |
| 3 | Trading Worker | **PARTIAL** (โค้ด+test ใกล้ COMPLETE, ขาด evidence การใช้งานจริง) | • PAPER default + fail-closed: `main.py:218` (`execution_mode: WorkerExecutionMode = WorkerExecutionMode.PAPER`), `main.py:630` (`os.getenv("EXECUTION_MODE","PAPER")`), พลาด ARM แล้วรีเซ็ตกลับ PAPER/DISARMED พร้อมถอด stream: `main.py:2384-2412`<br>• Persistence/outbox durable: transactional outbox `apps/trading_worker/persistence/manager.py:1`, writer+dispatcher task `manager.py:412-413`, `ensure_order_durable` ack จาก DB ก่อนส่งจริง `manager.py:609-648`, readiness เปิดเผย `pending_outbox` `manager.py:480-490`; startup ล้มเหลวแล้ว fail-closed `main.py:3714`<br>• Execution lease ป้องกัน duplicate executor: fencing token + retain high-water mark `apps/trading_worker/execution_lease.py:69-114`, บังคับเมื่อ K_SERVICE/Cloud Run `execution_lease.py:29-36`, adapter บังคับ lease บน MAINNET `venues/binance/execution.py:150-153`; migration `infra/postgres/migrations/002_execution_leases.sql`<br>• Reconciliation deterministic: `BinanceReconciliation` `venues/binance/reconciliation.py:486` (+fill recovery `_recover_recent_trades` :743, diff compare :865); gate 2 ชั้น `venues/binance/gates.py:210` (DecisionExecutionGate), `:341` (OrderExecutionGate)<br>• Stale บล็อกการกระทำ: เช็คอายุข้อมูลใน gates (`gates.py:188 _age_seconds`), ข้อความ preflight "missing or stale" `main.py:1958,2058-2065,2713`<br>• Private stream reconnect มี test: bounded reconnect loop `venues/binance/user_stream.py:270-298` + `tests/python/test_user_stream_recovery.py`<br>• Kill switch อิสระจาก AI queue: endpoint `main.py:611`, บล็อกก่อนทุก path `main.py:1465`, RECOVERY_ONLY mode `main.py:1487-1488,2197`, fail-closed หลัง execution error `main.py:3270`<br>• Mainnet ถูก gate ซ้อนหลายชั้น: adapter สร้างไม่ได้ถ้าไม่มี `MAINNET_LIVE_APPROVED=true` `execution.py:85-89`, Mainnet จำกัด ETHUSDC perp `execution.py:206-215`, restart fencing `main.py:3729`, release controller ตรวจ approval แบบ one-time/one-scope `infra/cloudrun/release_controller.py:229-245`<br>• pytest 471 passed + 32 test files ใน `tests/python/` (เช่น `test_execution_lease.py`, `test_mainnet_safety.py`, `test_persistence.py`, `test_user_stream_recovery.py`, `test_gate.py`) — อ้าง baseline | • Testnet contract/soak **evidence chain ถูก waive** โดย `docs/DECISION-2026-09-21-skip-testnet-evidence.md` (compensating controls: mainnet read-only preflight Gate 3, CLI research, staged first order Gate 5) — ความเสี่ยงรับไว้แล้ว แต่ยังไม่มีผลจากการรัน preflight/soak จริงมาอ้าง<br>• Staging UAT scenario §18 (restart, reconciliation mismatch, kill switch บน staging) ยังไม่มีบันทึกผล |
| 4 | Research / Backtest | **PARTIAL** | • Reproducibility: dataset manifest ผูก sha256 + canonical hash `apps/trading_worker/backtest/evidence_artifact.py:119-186` (`ResearchDatasetManifest`), `build_replay_evidence_artifact` :396 + `verify_replay_evidence_artifact` :448; typed research config `apps/trading_worker/config/research_config.py` + `tests/python/test_research_config.py`<br>• Cost model ชัดและ fail-closed: taker fee `backtest/replay.py:933` (`notional * cost_model.taker_fee_rate`), slippage/funding เป็น field บังคับ `replay.py:292-293`, funding_event ต้องมี funding_rate ไม่งั้น raise `replay.py:216-219`<br>• ไม่มี execution authority: `research/binance_cli.py:3` ระบุชัด "The Python Trading Worker remains the only execution authority"; `grep -rn "place_order\|submit_order\|create_order" apps/trading_worker/backtest/ apps/trading_worker/research/` → ไม่พบ<br>• Evidence ผูก commit: `BuildEvidence.build_sha` (`apps/trading_worker/evidence.py:10-14`), artifact ทดสอบผูก `build_sha` + บังคับ host testnet เท่านั้น (`evidence.py` TestnetTrialArtifact validators)<br>• Honesty labels: UI replay/analytics ติด `ILLUSTRATIVE_ONLY` banner (ดูหมายเลข 2) และ README:25-27 ประกาศว่า fixture ไม่ใช่ profitability evidence | • ยังไม่มี evidence artifact ผลวิจัยจริง (walk-forward/OOS/regime attribution) ถูก commit เป็นหลักฐาน — §3.4 "Lack of profitable evidence is accepted as a valid result" ยังไม่ถูกบันทึกเป็น artifact ใด ๆ<br>• การรัน backtest จริงเพื่อสร้าง artifact ยังไม่เกิดในรอบ audit นี้ (นอกขอบเขต) |
| 5 | Infrastructure / Release | **PARTIAL** | • CI เขียวบน main: `gh run list --limit 5` → run `35556513219` "Blessing AI CI" **success** บน commit `49d540c...` (2026-09-21) = commit เดียวกับ HEAD branch นี้<br>• CI-equivalent suite ตรง Plan §10/§21: `.github/workflows/ci.yml:21-58` (npm ci → dataconnect drift → lint → test → build → pip dev → ruff E9,F63,F7,F82 → pytest --cov-fail-under=65)<br>• Secret Manager: `infra/cloudrun/deploy.ps1:52` `--set-secrets POSTGRES_PASSWORD=...,BINANCE_MAINNET_API_KEY=...,BINANCE_MAINNET_API_SECRET=...`<br>• IAM least-privilege: dedicated SAs `blessing-control-plane` / `blessing-release-controller` `infra/cloudrun/provision-release-identities.ps1:18-19` + repo-scoped grants :106-120<br>• Deploy disarmed by default: `deploy.ps1:51` `EXECUTION_MODE=PAPER,PERSISTENCE_MODE=REQUIRED,EXECUTION_LEASE_REQUIRED=true,MAINNET_LIVE_APPROVED=false`; มี `deploy-live-disarmed.ps1`, `verify-disarmed.ps1`, `verify-live-disarmed.ps1`<br>• Monitoring/alerts: `infra/monitoring/alert-policies.json` (readiness, outbox failure/queue full, private stream, reconciliation drift, daily loss cap), `budget.json` thresholds 50/75/90/100% ตรง Plan §6.9 (budgetAmount 10, "alerts only, not a hard cap")<br>• Schema reproducible: migrations `infra/postgres/migrations/001..006`<br>• Rollback path มีใน runbook: kill switch → reconcile → pause/disarm `docs/MAINNET-RELEASE-RUNBOOK.md:250` | • **P0 ตาม Plan §6.5 ยังไม่มี tooling**: `infra/artifact-registry/` (audit-images.ps1, cleanup-policy.json, verify-protected-digests.ps1) — `ls infra/artifact-registry` → No such file; artifact retention/cleanup ไม่มีการคุมเลย<br>• `docs/COST-BASELINE-LIVE.md` (measure-first §6.1) ยังไม่มี; `docs/COST_MODEL.md` ยังเป็น projection ไม่ใช่ measured (หัวไฟล์ระบุตัวเองว่า "projected operational cost model")<br>• ไม่มีบันทึก Staging UAT (`docs/ZCODE-STAGING-UAT.md`) และไม่มีหลักฐาน rollback ถูก verify จริงบน staging<br>• ไม่มี ADR สถานะ DataConnect deferral ตาม Plan §17 Option A (มีแต่กลไก enforce flag=false ที่ `release.ts:358` + `docs/DATACONNECT-MIGRATION.md`) — ควรเขียน decision record ให้ชัดเหมือน `docs/DECISION-2026-09-21-skip-testnet-evidence.md`<br>• ไม่พบ literal state `READY_FOR_OPERATOR_APPROVAL` ในโค้ด (`git grep` → ไม่พบ) — สถานะนี้เป็น concept ของ runbook; กลไกจริงคือ `MAINNET_LIVE_APPROVED=false` + one-time approval consumption (ต้องชัดเจนว่า final report จะสรุปจากกลไก ไม่ใช่ string) |

---

## P0 ที่เห็นทันที (เรียงตามผลกระทบต่อการปิด v0.2)

1. **สร้าง `infra/artifact-registry/` cleanup/retention tooling ให้ครบ** ตาม Plan §6.5 — ปัจจุบันไม่มีไฟล์ใด ๆ (`ls infra/artifact-registry` → No such file) → เสี่ยง Artifact Registry โตเรื่อย ๆ (cost) และไม่มีการ protect digest ที่ใช้ rollback (recoverability)
2. **วัดต้นทุนจริงและสร้าง `docs/COST-BASELINE-LIVE.md`** (read-only gcloud/billing) ตาม §6.1 — ทุก decision เรื่อง cost ตอนนี้ยังพิง projection ใน `docs/COST_MODEL.md`
3. **สร้าง `docs/ZCODE-BACKLOG.md`** แปลง gap ทั้งหมดใน audit นี้เป็น backlog มี ID/priority/acceptance (Plan §11) — ป้องกัน gap หายเพราะไม่มี issue
4. **ปิด STALE/UNAVAILABLE evidence labeling ใน frontend** (§3.2) — เพิ่ม label/state เมื่อข้อมูลเกิน freshness window หรือ source unreachable แทนการแสดงข้อมูลเก่าแบบไม่มีป้าย
5. **Commit หรือ revert การแก้ `infra/monitoring/verify-release-monitoring.ps1` ที่ค้างใน working tree** และตัดสินใจว่าจะ track `Plan(4).md` หรือไม่ — ปัจจุบัน working tree ไม่สะอาด ขัด §3.1
6. **แก้ docs drift หัว README.md:1 ("Blessing AI v0.1")** ให้สอดคล้อง v0.2 พร้อมแยก PRODUCTION vs LOCAL dev (§20)
7. **เขียน ADR ยืนยัน DataConnect deferral เป็น v0.3** (Plan §17 Option A) — กลไก enforce มีแล้ว (`release.ts:358`) แต่ไม่มี decision record

P1 ที่ผูกกับการปิดรอบนี้: รัน Staging UAT ตาม §18 และบันทึก `docs/ZCODE-STAGING-UAT.md`; รัน mainnet read-only preflight (Gate 3) เพื่อสร้าง evidence ทดแทน testnet ตาม decision doc; เขียน `docs/ZCODE-FINAL-REPORT.md`.

---

## ข้อจำกัดของการตรวจ

- **ไม่ได้รัน pytest/npm test/build ซ้ำ** ตามกฎที่ได้รับ — ตัวเลข 471 passed และสถานะ PASS(0) ทั้งหมดอ้างจาก baseline ที่ script รันให้ก่อน audit นี้
- **ไม่ได้ตรวจ live GCP state ใด ๆ** (ไม่มีการเรียก gcloud แม้แต่แบบ read-only ในรอบนี้) — ข้อสรุปฝั่ง infra อิงเฉพาะ config ใน repo (`deploy.ps1`, `alert-policies.json`, `budget.json`) จึงไม่ทราบว่า deployment จริงบน Cloud Run ตรงกับ repo หรือไม่, ยอดเงินจริง/budget จริงบน billing ไม่ถูกยืนยัน
- **ไม่ได้ทดสอบ UI บน browser จริง** — "routes render / responsive layout" สรุปจากโค้ด route + vitest/build ผ่าน ไม่ใช่ UAT
- **GitHub CI เขียว** อ้างจาก `gh run list` ล่าสุดบน `main` (run 35556513219); branch `zcode/finish-blessing-v0.2` อยู่ที่ commit เดียวกับ `main` HEAD (49d540c) จึงยังไม่มี CI run ของ commit ใหม่เฉพาะ branch นี้
- **Git status เป็น snapshot วันที่ 2026-09-22** — ไฟล์ที่ untracked/modified อาจเปลี่ยนหลังจากนี้
- การอ่านไฟล์ `apps/trading_worker/venues/binance/{gates,execution,reconciliation}.py` เป็น **read-only เท่านั้น** ตามกฎ — ไม่มีการแก้ไขไฟล์ใดใน 3 ไฟล์นี้และไม่มีการแก้ไฟล์อื่นนอกจากเอกสารฉบับนี้

---

## Implementation update — 2026-09-22

The approved readiness gaps have now been implemented on this branch:

- `src/lib/evidence.ts` derives explicit `VERIFIED`, `SIMULATED`, `STALE`, and
  `UNAVAILABLE` status from server timestamps and worker responsiveness.
- `StartTradingWizard` now displays Authentication, Role, Worker heartbeat,
  and Persistence checks. Client role guidance mirrors the server hierarchy;
  server authorization remains authoritative.
- Control Plane deployment and verification scripts now use bounded runtime
  profiles with request-based CPU throttling; the Worker profile is unchanged.
- `infra/artifact-registry/` contains read-only image inventory and protected
  digest checks plus a conservative untagged-only policy. No policy was
  applied remotely.
- Live metadata is recorded in `docs/COST-BASELINE-LIVE.md`; Data Connect
  deferral, backlog, named staging UAT, and final report are now explicit.
- README, HTML title, and environment example v0.1 drift was corrected.

The current Cloud Run Control Plane remains a separately deployed revision
whose observed max scale and CPU policy do not yet match the new repository
profile. This is an operator rollout/read-back gate, not an implementation
failure, and no remote deployment was performed.

## Final verification update — 2026-09-22

- Implementation commit: `c37196f8e28050b35933ae71975b6610808485da`.
- Cross-platform lockfile correction: `12164338543f1b7874940dd1cb26da500c824660d`.
- Pull request: `https://github.com/kotorn/Blessing-AI/pull/38`.
- Current-SHA GitHub Actions run `35718992114` passed all jobs, including
  `npm ci` on Ubuntu/Node 24, generated SDK drift, TypeScript gates, Python
  coverage, and hygiene.
- Local rendered QA confirmed v0.2 identity, provenance labels, readiness
  checks, and a disabled ARM control in the unauthenticated/unavailable state.
- Remaining gates are authenticated role UAT, staging fault-injection evidence,
  separately approved remote profile rollout/read-back, Artifact Registry
  cleanup-policy approval, and PR review/merge approval.
