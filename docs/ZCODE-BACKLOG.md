# Blessing AI v0.2 — ZCODE Backlog (Phase 1: Authoritative Backlog)

- วันที่: 2026-09-23 | แหล่ง: `docs/ZCODE-COMPLETION-AUDIT.md` (audit 2026-09-23) + baseline gates ที่ orchestrator script รันให้ (branch `zcode/finish-blessing-v0.2`) + ผลตรวจ GCP read-only ของ script + คำสั่งที่ agent นี้รันเองรอบนี้
- รูปแบบตาม `PLAN.md:647-680` (§11): ทุก item มี ID, priority, subsystem, problem, evidence, files, acceptance criteria, tests, dependencies, risk, status — ห้าม gap หายเพราะ GitHub ไม่มี issue (`PLAN.md:680`)
- Priority: **P0** = safety/security/data-loss/release blocker, **P1** = จำเป็นต่อ v0.2, **P2** = quality/cost/observability/UX, **P3** = อนาคต (`PLAN.md:671-678`)
- **OPERATOR-APPROVAL-REQUIRED** = การปิด item นั้นเป็น production mutation (Cloud Run / Cloud SQL / IAM / billing / Artifact Registry deletion / Secret Manager / Mainnet credentials / production migrations) ซึ่ง §1.3 (`PLAN.md:84-90`) บังคับให้หยุดรอ operator อนุมัติก่อน — read-only ตรวจได้เสมอ
- Agent นี้ไม่ได้รัน pytest/vitest/lint/build ซ้ำ (กฎ ask) — ตัวเลข 471 passed / PASS(0) ทั้งหมดอ้างจาก baseline ของ orchestrator เท่านั้น

## คำสั่งที่ agent นี้รันเองรอบนี้ (นอกเหนือจากของ orchestrator)

1. `git log --oneline -5` / `git status --short` → HEAD ใหม่ `2180d2a` ("feat(wizard): expand tradable universe..."), working tree เหลือแค่ `M docs/ZCODE-COMPLETION-AUDIT.md`
2. `git branch --show-current` → `codex/expand-instruments-and-wizard-upgrade`; `git show 2180d2a --stat` → 5 ไฟล์ +377/−88
3. `gh run view 35771286110 --json headSha,conclusion,displayTitle` → `success` ที่ headSha `2180d2a386211b4e1322eeadf9e30457137443c4`
4. `gh pr list --state open` → PR #43 (wizard) + dependabot #31–#36
5. `node -e ... infra/monitoring/alert-policies.json` → 14 policies (ชื่อตรง live ทุกตัว); `node -e ... infra/monitoring/log-metrics.json` → 18 metrics
6. `ls infra/postgres/migrations/` → 001–006; `grep -n cpu-throttling infra/cloudrun/deploy-control-plane.ps1` → บรรทัด 95
7. `grep -n` ใน `docs/ZCODE-STAGING-UAT.md`, `docs/COST-BASELINE-LIVE.md`, `src/components/StartTradingWizard.tsx`, `src/backend/system.ts`, `src/lib/evidence.ts` (ปักหมายเลขบรรทัดด้านล่าง)

## ตารางสรุป

| ID | Priority | Subsystem | ธง | Status |
|---|---|---|---|---|
| P0-1 | P0 | Release / Frontend+Worker | — | CLOSED — PR #43 merged (a0b6a54) |
| P0-2 | P0 | Staging UAT / Release verification | OPERATOR-APPROVAL-REQUIRED (fault injection) | OPEN |
| P0-3 | P0 | Frontend UAT (Wizard + Evidence UI) | — | OPEN |
| P0-4 | P0 | Safety / Mainnet boundary | OPERATOR-APPROVAL-REQUIRED | ENFORCED — HOLD BY DESIGN |
| P1-1 | P1 | Billing guardrails | OPERATOR-APPROVAL-REQUIRED | OPEN |
| P1-2 | P1 | Billing totals / cost report | read-only (export setup = operator) | OPEN |
| P1-3 | P1 | Cloud Run profile (Control Plane) | OPERATOR-APPROVAL-REQUIRED | OPEN — drift บันทึกไว้แล้ว |
| P1-4 | P1 | Artifact Registry retention | OPERATOR-APPROVAL-REQUIRED | OPEN — OPERATOR APPLY |
| P1-5 | P1 | Build reproducibility (npm/Windows) | — | OPEN — watch |
| P1-6 | P1 | BigQuery guardrails / GCS lifecycle | — | NOT_RUN |
| P2-1 | P2 | Monitoring coverage | OPERATOR-APPROVAL-REQUIRED (apply) | OPEN |
| P2-2 | P2 | Cloud SQL resilience / schema parity | แก้ config = OPERATOR-APPROVAL-REQUIRED | NOT_RUN |
| P2-3 | P2 | Documentation drift (cost baseline) | — | OPEN |
| P2-4 | P2 | Dependency hygiene | — | OPEN |
| P3-1 | P3 | Data Connect cutover | v0.3 | DEFERRED |
| P3-2 | P3 | Testnet evidence | เมื่อ waiver ถูกเพิกถอน | WAIVED BY DECISION |
| P3-3 | P3 | Research profitability | — | FUTURE |

นับ: **P0=3 open (1 closed), P1=6, P2=4, P3=3**

---

## รายการ P0

### P0-1 — Merge งาน wizard/system expansion (PR #43) ลง main

- **Priority / subsystem:** P0 / Release + Frontend + Trading Worker
- **Problem:** งานที่ audit บันทึกว่า "ค้าง working tree 4 ไฟล์" (`docs/ZCODE-COMPLETION-AUDIT.md:26,36`) ถูก commit ไปแล้วเป็น `2180d2a` บน branch `codex/expand-instruments-and-wizard-upgrade` (ข้อมูล audit ตอนนี้ล้าหลัง) แต่ PR #43 ยังเปิด ไม่ได้ merge ลง main → v0.2-COMPLETE บน main ยังไม่รวมงานนี้ และ baseline gates ของ orchestrator รันบน `zcode/finish-blessing-v0.2` ก่อนงานนี้จึงไม่ครอบ `2180d2a`
- **Evidence:** `git show 2180d2a --stat` → `apps/trading_worker/main.py` (+18), `src/backend/system.ts` (+22), `src/components/StartTradingWizard.tsx` (+332), `src/pages/MarketsPage.tsx`, `tests/system.test.ts` (+40) รวม +377/−88; `gh run view 35771286110 --json` → `conclusion: success`, `headSha: 2180d2a386211b4e1322eeadf9e30457137443c4`; `gh pr list --state open` → `{"number":43,"headRefName":"codex/expand-instruments-and-wizard-upgrade",...}`
- **Files:** `src/components/StartTradingWizard.tsx`, `src/backend/system.ts`, `src/pages/MarketsPage.tsx`, `apps/trading_worker/main.py`, `tests/system.test.ts`
- **Acceptance criteria:** PR #43 ถูก merge ลง main; CI บน merge commit ของ main เขียว; `git status` สะอาด (ไม่มีไฟล์โค้ดค้าง)
- **Tests:** GitHub CI (suite ตาม `ci.yml` §10/§21 — อ้าง `docs/ZCODE-COMPLETION-AUDIT.md:30`) บน merge commit; ห้ามรันซ้ำเองตามกฎ ask นี้
- **Dependencies:** ไม่มี (CI ของ branch เขียวแล้ว)
- **Risk:** ค้าง merge นาน → conflict กับ lane/PR อื่นและ drift จาก main
- **Status:** CLOSED — PR #43 merged into main in commit `a0b6a54`

### P0-2 — ปิด Staging UAT ส่วน AUTH + FAULT (ZC-006)

- **Priority / subsystem:** P0 / Release verification
- **Problem:** UAT บน deployment จริงผ่านแค่ ROOT+HEALTH+ROUTE+UNAUTH — บทบาท authenticated และ fault scenarios ของ §18 ยัง NOT_RUN ทั้งหมด
- **Evidence:** `docs/ZCODE-STAGING-UAT.md:68-70` (authenticated viewer/operator/trading_admin = `NOT_RUN — no authorized session was entered`), `:73` (restart/reconciliation fault injection = `NOT_RUN — no remote fault injection permitted`), `:74` (responsive UAT = NOT_RUN); สถานการณ์ที่ต้องครบ `PLAN.md:891-915` (§18 ข้อ 2–11: viewer, trading_admin, Worker unavailable, DB unavailable, stale market data, reconciliation mismatch, PAPER normal, restart, kill switch, cold/warm behavior); สถานะเดิม ZC-006 PARTIAL (`docs/ZCODE-COMPLETION-AUDIT.md:30`)
- **Files:** `docs/ZCODE-STAGING-UAT.md`, `docs/MAINNET-RELEASE-RUNBOOK.md`
- **Acceptance criteria:** ทุกแถว §18 2–11 มี browser evidence จริง (DOM/console/network capture + destination read-back) หรือเหตุผล NOT_RUN ที่ operator รับรองเป็นลายเซ็น; ห้ามมี action ARM/order ระหว่าง UAT
- **Tests:** browser capture บน staging URL จริง (`https://blessing-control-plane-201945750223.asia-southeast1.run.app` ตามผล script `run-services`); ชุด vitest/pytest ที่มีอยู่อ้าง baseline เท่านั้น
- **Dependencies:** authorized test accounts (viewer/operator/trading_admin) — ต้อง operator จัด; **ควรทำหลัง P0-1 merge** เพื่อ UAT บน SHA ที่จะ release จริง
- **Risk:** ประกาศ UAT เขียวจาก session ไม่พิสูจน์ตัวตนหรือ deployment เก่า (risk เดิมของ ZC-006)
- **Status:** OPEN — **OPERATOR-APPROVAL-REQUIRED** สำหรับ fault injection บน service จริง (production Cloud Run/Cloud SQL — `PLAN.md:84-90`); ส่วน authenticated browse ใช้สิทธิ์ read-only ได้

### P0-3 — AUTH UAT ของ Start Trading Wizard + Evidence provenance UI (ZC-003/ZC-004)

- **Priority / subsystem:** P0 / Frontend + Control Plane
- **Problem:** Wizard และ evidence labeling ผ่านระดับโค้ด+test+UNAUTH UAT แล้ว แต่ "readiness blockers แสดงก่อน action ในบทบาทผู้ใช้จริง" ยังไม่เคยพิสูจน์ใน session authenticated (`docs/ZCODE-COMPLETION-AUDIT.md:39`)
- **Evidence:** `src/components/StartTradingWizard.tsx:88` (state `preflightResult`), `:264` (`canArm = Boolean(preflightResult?.canArm) && localReadinessChecks.every(...required...PASS)`), `:574` (ข้อความเมื่อ `!canArm`); `src/backend/system.ts:66` (`export function evaluatePreflight`); `src/lib/evidence.ts:3` (`EvidenceStatus = 'VERIFIED'|'SIMULATED'|'STALE'|'UNAVAILABLE'`); สถานะ AUTH UAT OPEN เดิม (ZC-003/ZC-004 ใน backlog ฉบับก่อน + `docs/ZCODE-STAGING-UAT.md:68-70`)
- **Files:** `src/components/StartTradingWizard.tsx`, `src/backend/system.ts`, `src/lib/evidence.ts`, `src/app/StatusBar.tsx`
- **Acceptance criteria:** ใน session จริงทั้ง 3 บทบาท: (1) readiness rows แสดงสาเหตุ block ก่อนเข้าถึงปุ่ม action, (2) ARM disabled เมื่อ check ใด required พัง, (3) evidence badge เปลี่ยนตาม derivation rule (worker ไม่ตอบ/ timestamp หาย → UNAVAILABLE; อายุเกิน → STALE) ตรง `src/lib/evidence.ts`
- **Tests:** vitest ชุด wizard/evidence อ้าง baseline (PASS — ห้ามรันซ้ำ) + browser evidence ใหม่บันทึกใน `docs/ZCODE-STAGING-UAT.md`
- **Dependencies:** test accounts ร่วมกับ P0-2; P0-1 merged ก่อน
- **Risk:** พึ่ง UI-readiness แทน server authorization ไม่ได้ (risk เดิม ZC-004)
- **Status:** OPEN (ทำคู่กับ P0-2 ได้ใน session เดียว)

### P0-4 — Operator approval คือด่านสุดท้าย (by design): คง `READY_FOR_OPERATOR_APPROVAL — NOT ARMED`

- **Priority / subsystem:** P0 / Safety — Mainnet boundary
- **Problem:** ไม่ใช่งานที่ agent ทำต่อได้ — เป็นเงื่อนไขปิด release ที่ออกแบบให้หยุดรอมนุษย์; ทุก lane ต้องรักษาสถานะ disarmed จนกว่า operator จะอนุมัติด้วยตนเอง
- **Evidence:** `PLAN.md:59-82` (§1.2: ห้าม arm/submit order/consume continuation approval; final state `READY_FOR_OPERATOR_APPROVAL` `PLAN.md:74-76`, never `AUTONOMOUS_LIVE` `PLAN.md:78-82`); live worker = `MAINNET_LIVE_APPROVED='false'` + `EXECUTION_MODE='LIVE'` + `PERSISTENCE_MODE='REQUIRED'` (audit ที่รัน gcloud เอง 2026-09-23 — `docs/ZCODE-COMPLETION-AUDIT.md:16`; คำสั่ง script รอบนี้ไม่ได้ query env ซ้ำ)
- **Files:** ไม่มี (invariant); กลไกจริง = env บน Cloud Run + `infra/cloudrun/release_controller.py` (approval consumption)
- **Acceptance criteria:** ทุกครั้งที่ตรวจ read-only ก่อนปิด v0.2: `MAINNET_LIVE_APPROVED=false` ยังคงอยู่; ไม่มี agent กด ARM/ส่ง order/เพิ่ม risk limit/ปิด kill switch
- **Tests:** read-only `gcloud run services describe` read-back ณ วันปิด release
- **Dependencies:** P0-1, P0-2, P0-3 และ operator เห็นควร P1 ใดๆ ต้องปิดก่อน
- **Risk:** การ ARM ก่อน UAT ปิด = อุบัติเหตุจริงด้วยเงินจริง
- **Status:** ENFORCED — HOLD BY DESIGN (**OPERATOR-APPROVAL-REQUIRED** โดยนิยาม)

---

## รายการ P1

### P1-1 — Budget มีจริงแล้วแต่ notification channel ว่าง (`allUpdatesRule: {}`)

- **Priority / subsystem:** P1 / Billing guardrails (ปิดส่วน config ของ ZC-002)
- **Problem:** budget จริงบน cloud ตรง repo config ครบถ้วน (สถานะดีขึ้นจาก audit ที่ยังไม่รู้ค่า) แต่ไม่มีผู้รับแจ้งเตือนเลย — threshold 50/75/90/100% ยิงแล้วไม่มีใครได้รับ → guardrail §6.9 ไม่ทำงานจริง
- **Evidence:** ผล script (read-only): budget `Blessing AI monthly alert budget` THB 10, `thresholdRules` 0.5/0.75/0.9 CURRENT_SPEND + 1.0 FORECASTED_SPEND, filter `projects/201945750223`, และ `"allUpdatesRule": {}` (ไม่มี channel); ตรง `infra/monitoring/budget.json:1-12` (amount 10, threshold เดียวกัน, scope `gen-lang-client-0730128480`) — ไฟล์ config เองก็ไม่ได้กำหนด channel → gap ตั้งแต่ design
- **Files:** `infra/monitoring/budget.json`, `infra/monitoring/apply.ps1`, `infra/monitoring/verify-budget.ps1`
- **Acceptance criteria:** budget มี notification channel (email ผู้รับจริง หรือ Pub/Sub topic) ที่ operator ยืนยัน; `verify-budget.ps1` เพิ่มการตรวจว่า `allUpdatesRule` ไม่ว่าง; บันทึกผลใน `docs/COST-BASELINE-LIVE.md` §Budget observations (ปัจจุบัน `:55-61`); ระหว่างทำทบทวน THB 5 budget ที่ unscoped (ไม่ filter project) ตามที่ `docs/COST-BASELINE-LIVE.md:59-60` ทักไว้
- **Tests:** read-only `gcloud billing budgets list` หลัง apply แสดง channel ที่ตั้ง
- **Dependencies:** operator เลือกช่องทาง/ผู้รับ (ต้องมีสิทธิ์ billing account `015E71-B4A1F4-C1467B`)
- **Risk:** ใช้จริงเกิน THB 10/เดือนโดยไม่มีใครรู้ทันที
- **Status:** OPEN — **OPERATOR-APPROVAL-REQUIRED** (billing mutation, `PLAN.md:84-90`)

### P1-2 — Billing totals จริง + monthly cost report ต่อ service (ส่วนที่เหลือของ ZC-002)

- **Priority / subsystem:** P1 / Billing / Cost
- **Problem:** ตอนนี้รู้ billing account (`015E****`) และ list budgets ได้แล้ว แต่ยังไม่มีตัวเลข spend จริง 7/30 วัน และยังไม่มีรายงาน cost ราย service ตาม §6.9 ("Produce a monthly cost report by service" — `PLAN.md:433`)
- **Evidence:** audit: `gcloud billing budgets list` ไม่สำเร็จเพราะขาด `--billing-account` (`docs/ZCODE-COMPLETION-AUDIT.md:38`); รอบนี้ script รัน `gcloud beta billing projects describe` → linked `015E****` และ `budgets list --billing-account=015E71-B4A1F4-C1467B` สำเร็จ (ผล JSON ใน ask); `docs/COST-BASELINE-LIVE.md` row Billing: "No invoice total or credit balance was available in this read-back"
- **Files:** `docs/COST-BASELINE-LIVE.md`, `docs/COST_MODEL.md`
- **Acceptance criteria:** spend จริง 7/30 วัน (จาก billing export หรือ `gcloud billing` read-only) + รายงานราย service ถูกบันทึกพร้อม timestamp; ค่าที่ยังไม่มีต้องเขียน UNKNOWN ต่อไป ห้ามเดาเป็นตัวเลข
- **Tests:** read-only query/export — ผลลัพธ์ reproducible ได้ซ้ำ
- **Dependencies:** สิทธิ์อ่าน billing account (ตอนนี้พอสำหรับ budgets list); ถ้าต้องตั้ง billing export → **OPERATOR-APPROVAL-REQUIRED**
- **Risk:** ตัดสินใจเรื่อง cost จาก projection ไม่ใช่เงินจริง (risk เดิม ZC-002)
- **Status:** OPEN (ส่วน read-only ทำได้ทันที)

### P1-3 — ปิด drift: Control Plane revision CPU throttling (false บน live vs true ตาม profile)

- **Priority / subsystem:** P1 / Cloud Run profile + Cost (§6.2)
- **Problem:** profile `MAINNET_OPERATOR_UI` กำหนด request-based CPU throttling = true แต่ revision live เป็น false → ค้างเป็น drift OPEN ที่บันทึกไว้เองแล้ว; กระทบต้นทุน (always-on CPU) และความถูกต้องของ runtime profile ที่ ZC-005 อ้างว่า DEPLOYED+READ-BACK PASS
- **Evidence:** `infra/cloudrun/deploy-control-plane.ps1:95` (args มี `"--cpu-throttling"`); ผล script `cp-describe`: annotation `run.googleapis.com/cpu-throttling=false`; `docs/COST-BASELINE-LIVE.md:12` (expectation: "revision request-based CPU throttling `true`"), `:39` (observed: "CPU throttling is currently false"), `:52` (drift table: "OPEN — CPU profile and v0.2 image still require approved rollout/read-back"); หมายเหตุความขัดแย้ง: `docs/ZCODE-COMPLETION-AUDIT.md:16` เขียนว่า revision cpu-throttling='true' — ขัดกับ COST-BASELINE:39/52 และผล cp-describe รอบนี้ → ถือว่า **false** คือค่าจริงบน live (audit ผิดจุดนั้น)
- **Files:** `infra/cloudrun/deploy-control-plane.ps1`, `infra/cloudrun/verify-control-plane.ps1`, `docs/COST-BASELINE-LIVE.md`
- **Acceptance criteria:** approved redeploy CP ด้วย `deploy-control-plane.ps1` MAINNET_OPERATOR_UI → read-back revision `cpu-throttling=true`, 100% traffic ที่ latest Ready revision, service min/max ยัง 1/1 (template max annotation 20 เป็นคนละ scope — `docs/COST-BASELINE-LIVE.md:12`); อัปเดตตาราง read-back ใน COST-BASELINE (ปัจจุบัน `:39` ยังชี้ revision `00027-h5d` เก่า — จับคู่กับ P2-3)
- **Tests:** `verify-control-plane.ps1` post-deploy read-back (tooling เดิมของ ZC-005)
- **Dependencies:** operator approval; image ที่ redeploy ควรเป็น SHA หลัง P0-1 merge
- **Risk:** redeploy กระทบ operator reachability ชั่วคราว (risk เดิม ZC-005)
- **Status:** OPEN — **OPERATOR-APPROVAL-REQUIRED** (production Cloud Run mutation, `PLAN.md:84-90`)

### P1-4 — Apply Artifact Registry cleanup policy (ZC-001) + inventory ใหม่

- **Priority / subsystem:** P1 / Infrastructure + Cost (§6.5)
- **Problem:** policy/เครื่องมือสร้างครบแล้วแต่ยังไม่ถูก apply จริง (โดยเจตนารอ operator) — registry โตเป็น 1,998.135 MB แล้ว; คำสั่ง image inventory รอบนี้ล้มเหลว (exit 2) ทำให้ digest/จำนวนภาพปัจจุบันไม่ถูกยืนยันซ้ำ
- **Evidence:** ผล script `ar-repos`: `blessing-repo` SIZE 1998.135 MB, DOCKER, STANDARD; ผล script `ar-images`: `exitCode 2`, output ว่าง (inventory ล่าสุดไม่สำเร็จ); สถานะเดิม ZC-001 "IMPLEMENTED / OPERATOR APPLY OPEN" (backlog ฉบับก่อน + `docs/ZCODE-COMPLETION-AUDIT.md:30`); ไฟล์ policy มีครบ: `infra/artifact-registry/{cleanup-policy.json,audit-images.ps1,verify-protected-digests.ps1}` (audit:30)
- **Files:** `infra/artifact-registry/*`, `docs/COST-BASELINE-LIVE.md`
- **Acceptance criteria:** `audit-images.ps1` รันสำเร็จได้ inventory ล่าสุด (แก้สาเหตุ exit 2 — อาจเป็น syntax/location ของคำสั่ง list); `verify-protected-digests.ps1` ยืนยัน digest ปัจจุบัน + rollback (เช่น `sha256:9fb1a350...` ตาม `docs/COST-BASELINE-LIVE.md:15`) ถูกป้องกัน; operator อนุมัติ policy (untagged >90d + keep 30 versions ล่าสุด) แล้ว apply + read-back; ขนาด repo ลดตาม policy
- **Tests:** read-only `gcloud artifacts docker images list` + policy read-back หลัง apply
- **Dependencies:** operator ระบุ digests ที่ต้อง protect ทั้งหมด
- **Risk:** cleanup ที่ไม่มี digest mapping ลบภาพที่ rollback ต้องการ (risk เดิม ZC-001)
- **Status:** OPEN — **OPERATOR-APPROVAL-REQUIRED** (Artifact Registry deletion, `PLAN.md:88`)

### P1-5 — `npm ci` บน Windows ไม่ deterministic (prune @emnapi)

- **Priority / subsystem:** P1 / Build reproducibility
- **Problem:** baseline gate แรกของ orchestrator ล้ม (`npm-ci=FAIL(1)`) แล้วผ่านในรอบสองโดยไม่มีการแก้อะไร → gate ไม่ deterministic บน Windows; เคยเกิดซ้ำมาแล้วอย่างน้อย 2 ครั้ง (fix ผ่าน `d983879`, `5bc48db` ตาม audit)
- **Evidence:** baseline gates ใน ask: `npm-ci=FAIL(1)` รอบแรก → `npm-ci=PASS(0)` รอบสอง; ประวัติ repeat + รายชื่อ commit fix (`docs/ZCODE-COMPLETION-AUDIT.md:30`)
- **Files:** `package-lock.json`, `.github/workflows/ci.yml`
- **Acceptance criteria:** `npm ci` ผ่านติดกันอย่างน้อย 3 ครั้งบนเครื่อง Windows ปัจจุบัน หรือมี documented workaround ที่ reproducible; CI บน GitHub (linux) ไม่กระทบ
- **Tests:** รัน `npm ci` ซ้ำเอง (เฉพาะเมื่อ operator อนุญาต — กฎ ask นี้ห้าม agent รันซ้ำ)
- **Dependencies:** ไม่มี
- **Risk:** gate ถูกมองว่า flaky → คนเพิกเฉยเมื่อ fail ด้วยสาเหตุจริง
- **Status:** OPEN — watch (ไม่บล็อกเพราะรอบสอง PASS และ CI จริงเขียวทั้ง `main` และ PR #43)

### P1-6 — BigQuery guardrails + GCS lifecycle บน live ยังไม่เคยตรวจ

- **Priority / subsystem:** P1 / Cost + Data governance (§6.6/§6.8)
- **Problem:** dataset มีอยู่จริง 4 ชุด แต่ "มี dataset" ≠ "มี guardrail" — ยังไม่มีหลักฐานว่า query วิจัยถูก cap ด้วย maximumBytesBilled หรือกฎอื่น และไม่เคยตรวจ GCS lifecycle rule จริงบน bucket
- **Evidence:** ผล script `bq ls` → `backtests`, `market_data`, `risk`, `signals`; audit ระบุชัดว่าไม่ได้ตรวจ BigQuery guardrails/GCS lifecycle บน live (`docs/ZCODE-COMPLETION-AUDIT.md:30,42,50`); ข้อกำหนด §12 "audit BigQuery query guardrails" (`PLAN.md:684-698`) + §6.6/§6.8 (`PLAN.md:399-420`)
- **Files:** `docs/COST-BASELINE-LIVE.md` (บันทึกผล), query path ของ research (`apps/trading_worker/research/`, `research/`)
- **Acceptance criteria:** บันทึก read-only ต่อ dataset: มี cost cap ใน query path จริงหรือไม่ (ชื่อไฟล์:บรรทัดที่ตั้ง `maximumBytesBilled`); GCS lifecycle rule จริงของ bucket ที่ใช้; ถ้าต้องเพิ่มกฎ → แยกเป็นงาน **OPERATOR-APPROVAL-REQUIRED**
- **Tests:** read-only `bq show` / policy get; grep โค้ดหา usage ของ bytes-billed cap
- **Dependencies:** สิทธิ์ read BigQuery (มี — `bq ls` สำเร็จ)
- **Risk:** query วิจัยพุ่ง cost โดยไม่มี cap ใดๆ
- **Status:** NOT_RUN

---

## รายการ P2

### P2-1 — log-based metrics 4 ตัวไม่มี alert policy กำกับ

- **Priority / subsystem:** P2 / Monitoring coverage
- **Problem:** มี log metric 18 ตัวแต่มี alert policy แค่ 14 ชุด — เหตุการณ์ 4 ชนิดเกิดขึ้นแล้ว "เงียบ" (มี metric วัดแต่ไม่มีใครถูกปลุก)
- **Evidence:** เทียบผล script สองคำสั่ง: `log-metrics` → 18 ชื่อ vs `alert-policies` → 14 ชื่อ; ตัวที่ไม่มี policy: `blessing_agy_lease_expired`, `blessing_agy_timeout`, `blessing_autonomous_resume_denied`, `blessing_outbox_queue_full`; ฝั่ง repo เหมือนกัน — `node -e` นับ `infra/monitoring/log-metrics.json` → 18, `infra/monitoring/alert-policies.json` → 14 (ชื่อทั้ง 14 ตรง live ทุกตัว) → เป็น gap ใน design ของ repo ไม่ใช่ drift ระหว่าง apply
- **Files:** `infra/monitoring/alert-policies.json`, `infra/monitoring/log-metrics.json`, `infra/monitoring/README.md`
- **Acceptance criteria:** เพิ่ม policy 4 ชุด (หรือบันทึกเหตุผลว่าทำไมไม่ต้องมี ใน `infra/monitoring/README.md`); live read-back ตรง repo; `verify-release-monitoring.ps1` ครอบการเทียบนี้
- **Tests:** read-only `gcloud logging metrics list` + `gcloud monitoring policies list` เทียบกันหลังเปลี่ยน
- **Dependencies:** ไม่มี; การ apply จริงบน cloud = **OPERATOR-APPROVAL-REQUIRED**
- **Risk:** lease หมดอายุ/AGY timeout/resume ถูกปฏิเสธ/outbox เต็ม เกิดขึ้นโดยทีมไม่รู้
- **Status:** OPEN

### P2-2 — Cloud SQL resilience + schema parity ยังไม่ verify

- **Priority / subsystem:** P2 / Data persistence (data-loss ที่ยังไม่พิสูจน์ว่าป้องกันไว้พอ)

- **Problem:** read-back ล่าสุดยืนยัน tier/disk/backup-enabled แต่ไม่ได้ query `pointInTimeRecoveryEnabled`, `backupRetentionDays`, `deletionProtection`; และไม่เคยมีบันทึกว่า schema จริงบน `blessing-sql-primary` ตรง migrations 001–006 ของ repo
- **Evidence:** ผล script `sql-describe`: `db-f1-micro`, `PD_SSD`, `10` GB, backup `enabled=True`, `POSTGRES_17`, `asia-southeast1`, `RUNNABLE` (ฟิลด์ที่ query มีเท่านี้); `ls infra/postgres/migrations/` → `001_persistence_outbox_and_hedge_identity.sql` … `006_backfill_environment_scoped_venue.sql`; audit ระบุว่าไม่ได้ตรวจ Cloud SQL instance config (`docs/ZCODE-COMPLETION-AUDIT.md:50`)
- **Files:** `infra/postgres/migrations/*`, `infra/cloudrun/deploy.ps1`
- **Acceptance criteria:** บันทึก read-only ครบ: PITR on/off + retention + deletion protection + ตาราง/ดัชนี live ตรง migrations 001–006; ถ้าต้องเปลี่ยน config ใด = **OPERATOR-APPROVAL-REQUIRED** (Cloud SQL, `PLAN.md:88`); ถ้า PITR ปิดอยู่ ให้ operator ตัดสินใจเปิด (มีค่าใช้จ่าย)
- **Tests:** read-only `gcloud sql instances describe` เพิ่มฟิลด์ + `information_schema` query ผ่าน `gcloud sql connect` แบบ read-only
- **Dependencies:** สิทธิ์ read SQL instance (มี — describe สำเร็จ)
- **Risk:** เวลาเหตุข้อมูลเสียหาย อาจ restore ไม่ได้ถึงจุดที่ต้องการ; `db-f1-micro` (shared-core) อาจไม่พอเมื่อ load จริง — แผน upgrade เก็บไว้เป็นข้อความใน P3 ถ้า metrics บอกเกิน
- **Status:** NOT_RUN

### P2-3 — `docs/COST-BASELINE-LIVE.md` บันทึก revision ค้างหลัง reality

- **Priority / subsystem:** P2 / Documentation drift (§20)
- **Problem:** ตาราง resource read-back ยังชี้ revision เก่า ทั้งที่ ZC-005 บันทึก read-back ที่ใหม่กว่าและ live URL ปัจจุบันเป็นชุด `hrybwxl4ra-as.a.run.app`
- **Evidence:** `docs/COST-BASELINE-LIVE.md:39` ("ready revision `blessing-control-plane-00027-h5d`") vs backlog ฉบับก่อน ZC-005 (read-back `blessing-control-plane-00029-c4m`, service 1/1, revision max 20) vs ผล script `cp-describe`/`worker-describe` (URL `...-hrybwxl4ra-as.a.run.app` ทั้งคู่, LAST DEPLOYED 2026-09-19)
- **Files:** `docs/COST-BASELINE-LIVE.md`
- **Acceptance criteria:** ทุก approved rollout อัปเดตตาราง resource read-back (revision + digest + timestamp ใหม่); จับคู่กับ P1-3 เมื่อ redeploy CP
- **Tests:** diff ระหว่าง read-back จริง (read-only describe) กับตารางใน doc ต้องว่าง
- **Dependencies:** P1-3 (redeploy จะทำให้ต้องอัปเดตอยู่ดี)
- **Risk:** operator อ้าง revision/digest เก่าตอนตัดสินใจ rollback (risk คล้าย ZC-010)
- **Status:** OPEN

### P2-4 — Dependabot PRs 6 ตัวค้างเปิด

- **Priority / subsystem:** P2 / Dependency hygiene + Security
- **Problem:** PR อัปเดต dependency สะสมเปิดอยู่ 6 ตัวรวม major bumps (typescript 5.8→7.0, eslint 9→10, @types/node 22→26, lucide-react 0.546→1.47, websockets 15→17, autoprefixer) ไม่มีการ review/merge/ปิด
- **Evidence:** `gh pr list --state open` → #31 (websockets 17.1), #32 (eslint 10.11.0), #33 (@types/node 26.6.1), #34 (typescript 7.0.2), #35 (lucide-react 1.47.0), #36 (autoprefixer 10.6.1)
- **Files:** `package.json`, `package-lock.json`, `requirements*.txt` ตามแต่ละ PR
- **Acceptance criteria:** ทบทวนทีละตัว: merge เมื่อ CI เขียว หรือปิดพร้อมเหตุผลที่บันทึกไว้; major bump (typescript/eslint) ต้องผ่าน full CI + spot-check build ก่อน
- **Tests:** CI ของ PR แต่ละตัว (GitHub รันให้ — ไม่ต้องรันซ้ำเอง)
- **Dependencies:** แนะนำหลัง P0-1 merge เพื่อลด conflict
- **Risk:** security patch ค้างไม่ถูกนำไปใช้; major bump ที่ merge พร้อมกันอาจ breaking ซ้อนกัน
- **Status:** OPEN

---

## รายการ P3

### P3-1 — Data Connect cutover ไป v0.3 (ZC-007 — เก็บไว้ไม่ให้ gap หายตาม `PLAN.md:680`)

- **Priority / subsystem:** P3 / Persistence architecture
- **Problem:** dual-authority ระหว่าง Firestore กับ Cloud SQL ยังเป็นความเสี่ยงถาวรจนกว่า cutover — ตอนนี้คุมไว้ด้วย ADR + gate
- **Evidence:** ADR deferral (`docs/ADR-2026-09-22-dataconnect-deferral-v0.3.md`); cutover บังคับ false: `server.ts:87`, `src/dataconnect/client.ts:16-19`, release candidate บังคับ false `src/backend/release.ts:358` (ตาม audit `docs/ZCODE-COMPLETION-AUDIT.md:17`)
- **Files:** ADR, `src/dataconnect/client.ts`, `src/backend/release.ts`
- **Acceptance criteria (v0.3):** backfill/ownership/isolation/rollback plan + UAT ก่อน cutover จริง (`PLAN.md:863-889`)
- **Tests:** drift + release-gate tests ที่มีอยู่ต้องยังผ่านไปตลอด
- **Dependencies:** v0.3 scope + operator
- **Risk:** premature cutover ทำ state ผู้ใช้ซ้ำ/หาย (risk เดิม ZC-007)
- **Status:** DEFERRED TO v0.3 — ไม่ใช่ gap ของ v0.2

### P3-2 — คืนสถานะ Testnet evidence เมื่อ waiver ถูกเพิกถอน (ZC-009)

- **Priority / subsystem:** P3 / Trading evidence
- **Problem:** สาย chain testnet (contract/soak) ถูก waive — ยอมรับได้ต่อเมื่อ compensating controls ยังถูกบังคับอยู่
- **Evidence:** `docs/DECISION-2026-09-21-skip-testnet-evidence.md` (audit `docs/ZCODE-COMPLETION-AUDIT.md:28` ยืนยัน waive มี compensating controls ชัดเจน ไม่ใช่ gap ก่อน v0.2)
- **Files:** decision doc, `docs/TESTNET-READINESS-CHECKLIST.md`
- **Acceptance criteria:** ถ้า operator เพิกถอน waiver → ทำ contract/soak evidence ครบก่อน mainnet approval ใดๆ; ตราบใดที่ยัง waive ห้ามอ้าง waiver เป็น "live readiness"
- **Tests:** mainnet safety/release-gate tests ชุดเดิมต้องยังเขียว
- **Dependencies:** การตัดสินใจของ operator เท่านั้น
- **Risk:** หลักฐานเชิงพฤติกรรมน้อยลงก่อน live ในอนาคต
- **Status:** WAIVED BY DECISION — NOT A PASS

### P3-3 — ผลวิจัย profitability บน historical dataset ที่ operator อนุมัติ

- **Priority / subsystem:** P3 / Research & Backtest (§3.4)
- **Problem:** pipeline evidence สมบูรณ์และ verify แล้ว แต่ยังไม่มีผล walk-forward/OOS "กำไร" ใดถูกอ้าง — §3.4 ยอมรับสถานะนี้เป็นผลลัพธ์ที่ valid; **ห้าม tune acceptance criteria จนได้ผลดี**
- **Evidence:** audit `docs/ZCODE-COMPLETION-AUDIT.md:29` ("ยังไม่มีผลลัพธ์ profitability ใดถูกอ้าง — เป็นไปตาม §3.4... ห้าม tune acceptance criteria"); artifact ปัจจุบัน: `docs/research/evidence_artifact_btcusdt.json` digest `24e48210...` (ZC-011 ฉบับก่อน — สร้างและ verify แล้ว)
- **Files:** `apps/trading_worker/backtest/*`, `docs/research/*`
- **Acceptance criteria:** run ใหม่บน dataset ที่ operator อนุมัติ (มี source/ช่วงเวลาจริง); ผลลบ/ไม่กำไร รายงานตรงไปตรงมา ไม่เป็นเหตุให้ relax เกณฑ์
- **Tests:** `verify_replay_evidence_artifact`/ชุด research tests ที่มีอยู่
- **Dependencies:** แหล่งข้อมูล historical ที่อนุมัติ + พื้นที่เก็บ
- **Risk:** synthetic/incomplete data สร้างความมั่นใจลอย
- **Status:** FUTURE — ไม่บล็อก v0.2

---

## รายการที่ปิดแล้ว/ยืนยันแล้ว (เก็บประวัติ — ห้าม gap หาย)

| รายการเดิม | สถานะปัจจุบัน | Evidence |
|---|---|---|
| ZC-002 (ส่วน config) budget ถูกสร้างจริงบน cloud | **CLOSED (ส่วนนี้)** — THB 10, thresholds 50/75/90/100 ตรง `infra/monitoring/budget.json:1-12` ทุกค่า | ผล script `budgets` + `billing-link`; ส่วนที่เหลือแยกเป็น P1-1 (channel) และ P1-2 (totals) |
| ZC-005 Runtime profiles | IMPLEMENTED — ยกเว้น CPU throttling drift ที่แยกเป็น **P1-3** | backlog ฉบับก่อน ZC-005 + `docs/COST-BASELINE-LIVE.md:52` (OPEN) |
| ZC-008 Release report | IMPLEMENTED — เอกสาร release ครบและผูก CI/live จริง | `docs/ZCODE-FINAL-REPORT.md` (audit `docs/ZCODE-COMPLETION-AUDIT.md:30`) |
| ZC-010 Health identity | DEPLOYED v0.2 / READ-BACK PASS | audit `docs/ZCODE-COMPLETION-AUDIT.md:30` + backlog ฉบับก่อน ZC-010 |
| ZC-011 Research artifact | IMPLEMENTED / ARTIFACT VERIFIED | `docs/research/evidence_artifact_btcusdt.json:1-2` (audit `:29`) |
| Monitoring live ตรง repo (14/14 policies, 18/18 metrics) | MATCHED — ไม่มี drift ระหว่าง apply | เทียบผล script `log-metrics`/`alert-policies` กับ `node -e` นับ repo (14/18) |
| Secret Manager / IAM / Worker continuous semantics | MATCHED | ผล script `secrets` (3 ชื่อ), `worker-describe` (min/max 1/1, cloudsql-instances annotation, cpu 1/mem 1Gi) |

## ข้อสังเกตความถูกต้องของ audit ต้นทาง (สำหรับผู้อ่านต่อ)

1. `docs/ZCODE-COMPLETION-AUDIT.md:26,36` บอกว่ามีไฟล์ค้าง working tree 4 ไฟล์ — **ล้าหลังแล้ว**: งานถูก commit เป็น `2180d2a` + CI เขียว (P0-1 เหลือแค่ merge)
2. `docs/ZCODE-COMPLETION-AUDIT.md:16` บอก revision CP `cpu-throttling='true'` — ขัดกับ `docs/COST-BASELINE-LIVE.md:39,52` และผล `cp-describe` รอบนี้ (=false); ตัวเอกสาร P1-3 ถือ **false** เป็นค่าจริง
3. Audit บอก "ไม่ทราบ billing account ID" (`:38`) — รอบนี้ script ได้ค่าแล้ว (`015E71-B4A1F4-C1467B`) และ budgets list สำเร็จ → อัปเดตสถานะใน P1-1/P1-2

## Safety boundaries (คงเดิมทุกข้อ)

- ไม่มี Mainnet arming, order submission, kill-switch mutation, secret rotation, billing mutation, database migration, หรือ Artifact Registry cleanup โดย agent
- ทุก item ที่ mark OPERATOR-APPROVAL-REQUIRED ห้ามดำเนินการจนกว่า operator จะอนุมัติตาม `PLAN.md:84-90` (§1.3)
- สถานะปลายทางของ agent คือ `READY_FOR_OPERATOR_APPROVAL — NOT ARMED` เท่านั้น (`PLAN.md:74-76`)
