แผน: ปลดล็อก workflow escalation และปล่อยให้รอบปรับปรุงรันจนจบ

สถานการณ์: SubAgent "ผู้แยก binance-service" ทำ extraction เสร็จแล้วอย่างถูกต้อง (ย้าย internals ของ verifyBinanceCredentials/fetchBinanceLiveBalances ไป src/backend/binance-verify.ts แบบ byte-identical, tsc ผ่าน, vitest 76/76, build ผ่าน, release-integrity 13/13) แต่ `npm run lint` exit 1 เพราะ ESLint เจอ error 8 จุดในไฟล์ `.zcode/workflow-runs/dwfrun-*.mjs` — ซึ่งเป็น artifact ที่ระบบ workflow สร้างเอง (untracked scratch) ไม่ใช่ source ของ repo และ agent ห้ามแตะทั้ง .zcode/ (กฎข้อ 3) และห้ามแก้ eslint config (กฎข้อ 4) จึงติดตายถาม orchestrator

การตัดสิน: ให้อนุญาตเพิ่ม `.zcode/` เข้า ignores ใน eslint.config.js เป็นข้อเดียว — เพราะเป็นการยกเว้น artifact ที่เครื่องมือสร้างเองในบริเวณเดียวกับ dist/ และ node_modules/ ไม่ใช่การปิด lint rule และไม่ใช่ ignore แบบกว้าง (ยัง lint source จริงทุกไฟล์) การเปลี่ยนนี้จะทำให้ gate `npm run lint` และ `eslint --max-warnings 0` (Gate G) วัดผลถูกต้องตลอดทั้ง pipeline

ขั้นตอนหลังอนุมัติ:
1. เรียก ResolveWorkflowQuestion(question_id="dwfq-a1fe84d0-1") ตอบ agent: อนุญาตเพิ่ม ignores 1 บรรทัด `.zcode/` ใน eslint.config.js (ห้ามแก้อย่างอื่น), รัน npm run lint ซ้ำให้ exit 0, แล้วรายงาน gatePassed=true พร้อมบันทึกการเปลี่ยนแปลงนี้ใน notes
2. ปล่อยให้ workflow (run dwfrun-a1fe84d0) เดินต่อจนถึง Phase 5.5 (commit) และ Phase 6 (รายงาน evidence)
3. เมื่อ run จบ สรุปผล + evidence ให้ผู้ใช้

ข้อจำกัดคงเดิม: ไม่แตะ money-path, ไม่ push/PR, subagent อื่นยังห้าม commit ตาม spec v2 ทุกข้อ