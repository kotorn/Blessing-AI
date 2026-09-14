"""Typed, git-SHA-bound evidence records used by Testnet launch readiness."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BuildEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    build_sha: str = Field(min_length=7)
    local_non_secret_tests_verified: bool = False
    github_ci_verified: bool = False
    readonly_contract_verified: bool = False
    manual_trial_verified: bool = False
    manual_trial_sha: Optional[str] = None
    testnet_soak_verified: bool = False

    @model_validator(mode="after")
    def validate_manual_trial_sha(self):
        if self.manual_trial_verified and self.manual_trial_sha != self.build_sha:
            raise ValueError("manual_trial_sha must match build_sha when manual trial is verified")
        return self


class TestnetTrialArtifact(BaseModel):
    """Sanitized artifact contract; credentials and raw responses are excluded."""

    model_config = ConfigDict(extra="forbid")

    build_sha: str = Field(min_length=7)
    environment: Literal["BINANCE_TESTNET"]
    rest_host: str
    ws_host: str
    symbol: str
    client_order_id: str
    exchange_order_id: Optional[str] = None
    submit_latency_ms: Optional[float] = None
    ws_latency_ms: Optional[float] = None
    order_lifecycle: List[str] = Field(default_factory=list)
    modify_result: str
    cancel_result: str
    fill_count: int = 0
    position_before: List[Dict[str, Any]] = Field(default_factory=list)
    position_after: List[Dict[str, Any]] = Field(default_factory=list)
    reconciliation_status: str
    diff_count: int
    status: Literal["PASS", "FAIL"]

    @field_validator("rest_host")
    @classmethod
    def validate_rest_host(cls, value: str) -> str:
        if value.rstrip("/") != "https://testnet.binancefuture.com":
            raise ValueError("Trial artifacts must use the Binance Testnet REST host")
        return value

    @field_validator("ws_host")
    @classmethod
    def validate_ws_host(cls, value: str) -> str:
        if value.rstrip("/") != "wss://stream.binancefuture.com/ws":
            raise ValueError("Trial artifacts must use the Binance Testnet WS host")
        return value


class TestnetSoakArtifact(BaseModel):
    """Sanitized artifact contract for supervised Testnet soak run."""

    model_config = ConfigDict(extra="forbid")

    build_sha: str = Field(min_length=7)
    environment: Literal["BINANCE_TESTNET"]
    strategy: str
    symbol: str
    runtime_seconds: float
    market_events_count: int = 0
    decision_count: int = 0
    intent_count: int = 0
    risk_approved_count: int = 0
    risk_rejected_count: int = 0
    orders_submitted_count: int = 0
    fills_count: int = 0
    total_fees_usdt: float = 0.0
    slippage_bps: float = 0.0
    max_exposure_notional: float = 0.0
    stream_disconnect_count: int = 0
    stream_reconnect_count: int = 0
    reconciliation_runs_count: int = 0
    reconciliation_diffs_count: int = 0
    reconciliation_status: str = "UNKNOWN"
    open_orders_remaining: int = 0
    ambiguous_requests_count: int = 0
    duplicate_fills_count: int = 0
    final_positions: List[Dict[str, Any]] = Field(default_factory=list)
    hard_stop_triggered: bool = False
    hard_stop_reason: Optional[str] = None
    status: Literal["PASS", "FAIL"]

