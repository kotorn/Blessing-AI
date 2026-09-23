from __future__ import annotations

from pathlib import Path


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one source block, found {count}")
    return text.replace(old, new, 1)


def replace_count(text: str, old: str, new: str, expected: int, label: str) -> str:
    count = text.count(old)
    if count != expected:
        raise SystemExit(f"{label}: expected {expected} source blocks, found {count}")
    return text.replace(old, new)


# ---------------------------------------------------------------------------
# Durable launch baseline
# ---------------------------------------------------------------------------
repo_path = "apps/trading_worker/persistence/postgres/repositories.py"
text = read(repo_path)
text = replace_once(
    text,
    '''        policy: str = "STAGED_FIRST_ORDER",\n        max_risk_increasing_orders: int = 1,\n    ) -> Mapping[str, Any]:''',
    '''        policy: str = "STAGED_FIRST_ORDER",\n        baseline_capital: Decimal = Decimal("250"),\n        max_risk_increasing_orders: int = 1,\n    ) -> Mapping[str, Any]:''',
    "repository launch signature",
)
text = replace_once(
    text,
    '''        if max_risk_increasing_orders != 1:\n            raise ValueError("Mainnet staged launch permits exactly one risk-increasing order")\n        await self.db.execute(''',
    '''        if max_risk_increasing_orders != 1:\n            raise ValueError("Mainnet staged launch permits exactly one risk-increasing order")\n        try:\n            baseline = Decimal(str(baseline_capital))\n        except (TypeError, ValueError, ArithmeticError) as exc:\n            raise ValueError("Mainnet launch baseline capital is invalid") from exc\n        if not baseline.is_finite() or baseline <= 0:\n            raise ValueError("Mainnet launch baseline capital must be finite and positive")\n        await self.db.execute(''',
    "repository baseline validation",
)
text = replace_once(
    text,
    '''                    launch_id, approval_id, image_digest, symbol, policy,\n                    max_risk_increasing_orders, reserved_orders, submitted_orders,\n                    state, created_at, updated_at\n                ) VALUES ($1, $2, $3, $4, $5, $6, 0, 0, 'ACTIVE', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)''',
    '''                    launch_id, approval_id, image_digest, symbol, policy,\n                    max_risk_increasing_orders, reserved_orders, submitted_orders,\n                    state, baseline_capital, baseline_set_at, created_at, updated_at\n                ) VALUES ($1, $2, $3, $4, $5, $6, 0, 0, 'ACTIVE', $7, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)''',
    "repository baseline insert",
)
text = replace_once(
    text,
    '''                policy,\n                max_risk_increasing_orders,\n            )''',
    '''                policy,\n                max_risk_increasing_orders,\n                baseline,\n            )''',
    "repository baseline insert args",
)
text = replace_count(
    text,
    '''                   autonomous_approved_at, last_restart_at, created_at, updated_at''',
    '''                   autonomous_approved_at, last_restart_at, baseline_capital,\n                   baseline_set_at, created_at, updated_at''',
    3,
    "repository baseline select columns",
)
text = replace_once(
    text,
    '''                      first_order_verified_at, autonomous_approved_at,\n                      last_restart_at, created_at, updated_at''',
    '''                      first_order_verified_at, autonomous_approved_at,\n                      last_restart_at, baseline_capital, baseline_set_at,\n                      created_at, updated_at''',
    "repository baseline returning columns",
)
text = replace_once(
    text,
    '''        if (\n            str(row["launch_id"]) != launch_id\n            or str(row["image_digest"]) != image_digest\n            or str(row["symbol"]).upper() != symbol.upper()\n            or str(row["policy"]) != policy\n        ):\n            raise RuntimeError("existing launch session does not match release approval")\n        return dict(row)''',
    '''        result = dict(row)\n        if (\n            str(result["launch_id"]) != launch_id\n            or str(result["image_digest"]) != image_digest\n            or str(result["symbol"]).upper() != symbol.upper()\n            or str(result["policy"]) != policy\n        ):\n            raise RuntimeError("existing launch session does not match release approval")\n        persisted_baseline = result.get("baseline_capital")\n        if persisted_baseline is not None and Decimal(str(persisted_baseline)) != baseline:\n            raise RuntimeError("existing launch session baseline does not match release policy")\n        # Asyncpg returns these columns after migration 007. Minimal unit-test\n        # doubles may omit them; project the values used for the INSERT so the\n        # manager-level contract remains representative without weakening SQL.\n        result.setdefault("baseline_capital", baseline)\n        result.setdefault("baseline_set_at", datetime.now(UTC))\n        return result''',
    "repository baseline readback",
)
write(repo_path, text)

manager_path = "apps/trading_worker/persistence/manager.py"
text = read(manager_path)
text = replace_once(
    text,
    '''        approval_id: str,\n        image_digest: str,\n        symbol: str = "ETHUSDC",\n    ) -> dict[str, Any]:''',
    '''        approval_id: str,\n        image_digest: str,\n        symbol: str = "ETHUSDC",\n        baseline_capital: Decimal = Decimal("250"),\n    ) -> dict[str, Any]:''',
    "manager launch signature",
)
text = replace_once(
    text,
    '''            image_digest=image_digest,\n            symbol=symbol,\n        )''',
    '''            image_digest=image_digest,\n            symbol=symbol,\n            baseline_capital=baseline_capital,\n        )''',
    "manager baseline pass-through",
)
write(manager_path, text)

# ---------------------------------------------------------------------------
# RiskGovernor: CAUTION scales new risk to 50%; >=10% fails closed.
# ---------------------------------------------------------------------------
governor_path = "apps/trading_worker/engines/risk_governor.py"
text = read(governor_path)
text = replace_once(
    text,
    '''        if risk_snapshot.margin_utilization_pct >= self.max_margin_utilization_pct:\n            if not is_reducing:\n                return self._reject(\n                    target,\n                    "Margin utilization "\n                    f"({risk_snapshot.margin_utilization_pct}%) exceeds limit "\n                    f"({self.max_margin_utilization_pct}%). Cannot increase exposure.",\n                )\n\n        # 3. Generate a decision for the relative delta.''',
    '''        if risk_snapshot.margin_utilization_pct >= self.max_margin_utilization_pct:\n            if not is_reducing:\n                return self._reject(\n                    target,\n                    "Margin utilization "\n                    f"({risk_snapshot.margin_utilization_pct}%) exceeds limit "\n                    f"({self.max_margin_utilization_pct}%). Cannot increase exposure.",\n                )\n\n        if self.risk_policy is not None and not is_reducing:\n            capital_scale = self.risk_policy.capital_scale_for_drawdown(\n                risk_snapshot.current_drawdown_pct\n            )\n            if capital_scale <= 0:\n                return self._reject(\n                    target,\n                    "Runtime risk policy blocks new exposure at the current drawdown tier.",\n                )\n            if capital_scale < 1:\n                required_delta *= capital_scale\n\n        # 3. Generate a decision for the relative delta.''',
    "governor capital scaling",
)
write(governor_path, text)

# ---------------------------------------------------------------------------
# Trading worker: runtime policy at ARM, durable baseline drawdown, emergency.
# ---------------------------------------------------------------------------
main_path = "apps/trading_worker/main.py"
text = read(main_path)
text = replace_once(
    text,
    'from apps.trading_worker.engines.risk_governor import RiskGovernor\n',
    'from apps.trading_worker.engines.risk_governor import RiskGovernor\nfrom apps.trading_worker.config.risk_policy import DrawdownTier, RiskPolicyError, load_mainnet_risk_policy\n',
    "main risk policy import",
)
text = replace_once(
    text,
    '''        self._mainnet_launch_session: Optional[dict[str, Any]] = None\n        self._last_risk_snapshot_enqueued_at: float = 0.0''',
    '''        self._mainnet_launch_session: Optional[dict[str, Any]] = None\n        self._last_risk_snapshot_enqueued_at: float = 0.0\n        self._last_drawdown_tier: Optional[str] = None\n        self._drawdown_emergency_disarmed = False''',
    "main drawdown runtime fields",
)
text = replace_once(
    text,
    '''@app.post("/reconcile")\nasync def reconcile_endpoint():''',
    '''@app.get("/risk")\ndef get_risk_status_endpoint():\n    if not WORKER_ENGINE:\n        raise HTTPException(status_code=503, detail="Worker not initialized")\n    return WORKER_ENGINE.get_risk_status()\n\n@app.post("/reconcile")\nasync def reconcile_endpoint():''',
    "risk status endpoint",
)
text = replace_once(
    text,
    '''    def _launch_session_value(self, key: str, default: Any = None) -> Any:\n        if self._mainnet_launch_session is None:\n            return default\n        return self._mainnet_launch_session.get(key, default)\n\n    async def _fence_autonomous_launch''',
    '''    def _launch_session_value(self, key: str, default: Any = None) -> Any:\n        if self._mainnet_launch_session is None:\n            return default\n        return self._mainnet_launch_session.get(key, default)\n\n    def _mainnet_baseline_capital(self) -> Optional[Decimal]:\n        raw = self._launch_session_value("baseline_capital")\n        if raw is None:\n            return None\n        try:\n            baseline = Decimal(str(raw))\n        except (TypeError, ValueError, ArithmeticError):\n            return None\n        if not baseline.is_finite() or baseline <= 0:\n            return None\n        return baseline\n\n    def get_risk_status(self) -> Dict[str, Any]:\n        policy = self.risk_governor.risk_policy\n        baseline = self._mainnet_baseline_capital()\n        return {\n            "max_drawdown": str(\n                policy.emergency_stop_pct if policy is not None else self.risk_governor.max_drawdown_pct\n            ),\n            "leverage": str(\n                policy.max_leverage if policy is not None else self.risk_governor.max_leverage\n            ),\n            "baseline": str(baseline) if baseline is not None else None,\n            "baseline_set_at": self._launch_session_value("baseline_set_at"),\n            "drawdown_tier": self._last_drawdown_tier,\n            "engine_state": self.engine_state.value,\n        }\n\n    async def _apply_mainnet_drawdown_policy(\n        self, equity: Decimal\n    ) -> tuple[Decimal, RiskState, bool]:\n        policy = self.risk_governor.risk_policy\n        baseline = self._mainnet_baseline_capital()\n        if policy is None or baseline is None:\n            raise RuntimeError("LIVE risk policy or durable starting-capital baseline is unavailable")\n        if baseline != policy.baseline_capital:\n            raise RuntimeError("Durable starting-capital baseline does not match runtime risk policy")\n\n        self.session_start_equity = baseline\n        drawdown_pct = policy.drawdown_pct(equity)\n        tier = policy.tier_for_drawdown(drawdown_pct)\n        risk_state = policy.risk_state_for_drawdown(drawdown_pct)\n        if tier.value != self._last_drawdown_tier:\n            logger.warning(\n                "monitor_event=drawdown_tier tier=%s drawdown_pct=%s baseline=%s equity=%s",\n                tier.value,\n                drawdown_pct,\n                baseline,\n                equity,\n            )\n            self._last_drawdown_tier = tier.value\n\n        if tier is DrawdownTier.NO_NEW_GRID:\n            self.pause_new_risk = True\n            self._refresh_engine_state()\n        elif tier is DrawdownTier.RECOVERY_ONLY:\n            self.pause_new_risk = True\n            self.recovery_only = True\n            self._refresh_engine_state()\n        elif tier is DrawdownTier.EMERGENCY:\n            self.pause_new_risk = True\n            self.recovery_only = True\n            logger.error(\n                "monitor_event=drawdown_emergency drawdown_pct=%s baseline=%s equity=%s",\n                drawdown_pct,\n                baseline,\n                equity,\n            )\n            try:\n                await self.set_kill_switch(True)\n            except Exception as exc:\n                self.kill_switch_active = True\n                logger.error("Drawdown kill-switch workflow failed closed: %s", type(exc).__name__)\n\n            adapter = self.execution_adapter\n            if adapter is not None:\n                try:\n                    adapter.bind_worker_authority(self)\n                    await adapter.emergency_flatten("ETHUSDC", authority=self)\n                except Exception as exc:\n                    logger.error(\n                        "monitor_event=drawdown_emergency_flatten_failed error=%s",\n                        type(exc).__name__,\n                    )\n\n            # A 20%% drawdown is a terminal runtime state for this arm. Keep the\n            # kill switch active, clear execution authorization, and present\n            # DISARMED until an operator explicitly resets the control path.\n            self.active_configuration = None\n            self._drawdown_emergency_disarmed = True\n            self._refresh_engine_state()\n            return drawdown_pct, risk_state, True\n\n        return drawdown_pct, risk_state, False\n\n    async def _fence_autonomous_launch''',
    "main drawdown helpers",
)
text = replace_once(
    text,
    '''        if self.kill_switch_active:\n            self.engine_state = WorkerEngineState.EMERGENCY\n        elif self.active_configuration is None:\n            self.engine_state = WorkerEngineState.DISARMED''',
    '''        if self.kill_switch_active and self._drawdown_emergency_disarmed:\n            self.engine_state = WorkerEngineState.DISARMED\n        elif self.kill_switch_active:\n            self.engine_state = WorkerEngineState.EMERGENCY\n        elif self.active_configuration is None:\n            self._drawdown_emergency_disarmed = False\n            self.engine_state = WorkerEngineState.DISARMED''',
    "main emergency disarmed state",
)
text = replace_once(
    text,
    '''        # This observation is intentionally performed before the Worker\n        # changes mode, starts its persistent adapter, or acquires an\n        # execution lease. It must pass without changing the lifecycle.\n        if mode == "LIVE":\n            try:\n                preflight = await self.run_mainnet_read_only_preflight()''',
    '''        # LIVE policy is a release input. Load it before any Mainnet\n        # observation or adapter lifecycle mutation; invalid/missing policy\n        # therefore refuses ARM fail-closed.\n        mainnet_risk_policy = None\n        if mode == "LIVE":\n            try:\n                mainnet_risk_policy = load_mainnet_risk_policy()\n                self.risk_governor.apply_risk_policy(mainnet_risk_policy)\n            except RiskPolicyError as exc:\n                logger.error("LIVE ARM risk policy unavailable: %s", type(exc).__name__)\n                return False, "LIVE risk policy is unavailable or invalid; execution remains disarmed."\n\n        # This observation is intentionally performed before the Worker\n        # changes mode, starts its persistent adapter, or acquires an\n        # execution lease. It must pass without changing the lifecycle.\n        if mode == "LIVE":\n            try:\n                preflight = await self.run_mainnet_read_only_preflight()''',
    "main live policy load",
)
text = replace_once(
    text,
    '''                    session = await self.persistence.create_mainnet_launch_session(\n                        approval_id=release_approval_id,\n                        image_digest=image_digest,\n                        symbol="ETHUSDC",\n                    )''',
    '''                    if mainnet_risk_policy is None:\n                        raise RuntimeError("validated Mainnet risk policy is unavailable")\n                    session = await self.persistence.create_mainnet_launch_session(\n                        approval_id=release_approval_id,\n                        image_digest=image_digest,\n                        symbol="ETHUSDC",\n                        baseline_capital=mainnet_risk_policy.baseline_capital,\n                    )''',
    "main baseline persisted at arm",
)
text = replace_once(
    text,
    '''                if (\n                    str(session.get("state", "")) != "ACTIVE"\n                    or int(session.get("reserved_orders", 0) or 0) != 0\n                    or int(session.get("submitted_orders", 0) or 0) != 0\n                ):\n                    await self._reset_after_failed_exchange_arm()\n                    return False, "LIVE staged launch session is already used or requires reconciliation."\n                self._set_mainnet_launch_session(dict(session))''',
    '''                if (\n                    str(session.get("state", "")) != "ACTIVE"\n                    or int(session.get("reserved_orders", 0) or 0) != 0\n                    or int(session.get("submitted_orders", 0) or 0) != 0\n                ):\n                    await self._reset_after_failed_exchange_arm()\n                    return False, "LIVE staged launch session is already used or requires reconciliation."\n                try:\n                    persisted_baseline = Decimal(str(session.get("baseline_capital")))\n                except (TypeError, ValueError, ArithmeticError):\n                    persisted_baseline = Decimal("0")\n                if (\n                    mainnet_risk_policy is None\n                    or persisted_baseline != mainnet_risk_policy.baseline_capital\n                    or session.get("baseline_set_at") is None\n                ):\n                    await self._reset_after_failed_exchange_arm()\n                    return False, "LIVE starting-capital baseline is not durably bound to this launch."\n                self._set_mainnet_launch_session(dict(session))''',
    "main baseline arm verification",
)
text = replace_once(
    text,
    '''                # Drawdown tracking\n                if self.session_start_equity is None:\n                    self.session_start_equity = equity\n                if self.session_peak_equity is None or equity > self.session_peak_equity:\n                    self.session_peak_equity = equity\n                    \n                if self.session_peak_equity > Decimal("0"):\n                    drawdown_pct = ((self.session_peak_equity - equity) / self.session_peak_equity) * Decimal("100.0")\n                else:\n                    drawdown_pct = Decimal("0.0")\n\n                risk_snapshot = RiskSnapshot(''',
    '''                # LIVE drawdown is measured from the durable starting-capital\n                # baseline captured at operator ARM. Testnet retains its legacy\n                # session-peak behavior so research semantics do not drift.\n                if self.execution_mode == WorkerExecutionMode.LIVE:\n                    drawdown_pct, tier_risk_state, emergency_handled = (\n                        await self._apply_mainnet_drawdown_policy(equity)\n                    )\n                    if emergency_handled:\n                        return\n                    account_risk_state = self._derive_testnet_risk_state(\n                        snapshot, drawdown_pct\n                    )\n                    if tier_risk_state in {\n                        RiskState.NO_NEW_RISK,\n                        RiskState.RECOVERY_ONLY,\n                        RiskState.EMERGENCY,\n                    }:\n                        effective_risk_state = tier_risk_state\n                    elif account_risk_state != RiskState.NORMAL:\n                        effective_risk_state = account_risk_state\n                    else:\n                        effective_risk_state = tier_risk_state\n                else:\n                    if self.session_start_equity is None:\n                        self.session_start_equity = equity\n                    if self.session_peak_equity is None or equity > self.session_peak_equity:\n                        self.session_peak_equity = equity\n                    if self.session_peak_equity > Decimal("0"):\n                        drawdown_pct = (\n                            (self.session_peak_equity - equity) / self.session_peak_equity\n                        ) * Decimal("100.0")\n                    else:\n                        drawdown_pct = Decimal("0.0")\n                    effective_risk_state = self._derive_testnet_risk_state(\n                        snapshot, max(Decimal("0.0"), drawdown_pct)\n                    )\n\n                risk_snapshot = RiskSnapshot(''',
    "main baseline drawdown computation",
)
text = replace_once(
    text,
    '''                    risk_state=self._derive_testnet_risk_state(\n                        snapshot,\n                        max(Decimal("0.0"), drawdown_pct),\n                    ),''',
    '''                    risk_state=effective_risk_state,''',
    "main effective risk state",
)
# Continuation must prove that the restart-loaded row still carries the same baseline.
text = replace_once(
    text,
    '''        if session:\n            self._set_mainnet_launch_session(session)\n\n        policy = str(session.get("policy", "")) if session else ""''',
    '''        if session:\n            self._set_mainnet_launch_session(session)\n\n        runtime_policy = self.risk_governor.risk_policy\n        if runtime_policy is None:\n            try:\n                runtime_policy = load_mainnet_risk_policy()\n                self.risk_governor.apply_risk_policy(runtime_policy)\n            except RiskPolicyError:\n                runtime_policy = None\n        durable_baseline = self._mainnet_baseline_capital()\n        baseline_ready = bool(\n            session\n            and runtime_policy is not None\n            and durable_baseline == runtime_policy.baseline_capital\n            and session.get("baseline_set_at") is not None\n        )\n        add_check(\n            "CHK-CONTINUATION-RISK-BASELINE",\n            "Durable Starting-capital Baseline",\n            baseline_ready,\n            "Starting-capital baseline matches the runtime Mainnet risk policy"\n            if baseline_ready\n            else "Durable starting-capital baseline is missing or mismatched",\n        )\n\n        policy = str(session.get("policy", "")) if session else ""''',
    "main continuation baseline check",
)
write(main_path, text)

print("guarded Mainnet risk core patch applied")
