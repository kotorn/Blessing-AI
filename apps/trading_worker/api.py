from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from enum import Enum
import logging
from domain.models import utc_now

logger = logging.getLogger("blessing.worker.api")

app = FastAPI(title="Blessing AI Worker Control API")

class WorkerExecutionMode(str, Enum):
    PAPER = "PAPER"
    TESTNET = "TESTNET"

class WorkerEngineState(str, Enum):
    DISARMED = "DISARMED"
    ARMING = "ARMING"
    ARMED = "ARMED"
    PAUSED_NEW_RISK = "PAUSED_NEW_RISK"
    RECOVERY_ONLY = "RECOVERY_ONLY"
    DEGRADED = "DEGRADED"
    EMERGENCY = "EMERGENCY"

class WorkerRuntimeState(BaseModel):
    execution_mode: WorkerExecutionMode
    engine_state: WorkerEngineState
    connection_state: str
    market_data_healthy: bool
    private_stream_healthy: bool
    authenticated: bool
    reconciliation_status: str
    kill_switch_active: bool
    pause_new_risk: bool
    recovery_only: bool
    active_configuration: Optional[dict] = None
    updated_at: datetime

# Global reference to the main execution engine / loop
WORKER_ENGINE = None

@app.get("/health")
def health_check():
    return {"status": "ok", "timestamp": utc_now()}

@app.get("/state", response_model=WorkerRuntimeState)
def get_state():
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    return WORKER_ENGINE.get_state()

@app.get("/capabilities")
def get_capabilities():
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    return WORKER_ENGINE.get_capabilities()

@app.post("/arm")
async def arm(config: dict):
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    success, msg = await WORKER_ENGINE.arm(config)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"status": "ARMED"}

@app.post("/disarm")
async def disarm():
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    await WORKER_ENGINE.disarm()
    return {"status": "DISARMED"}


@app.get("/preflight")
def get_preflight(execution_mode: str = "PAPER"):
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    return WORKER_ENGINE.get_preflight(execution_mode)

@app.post("/pause-new-risk")
async def pause_new_risk(req: dict):
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    active = req.get("active", True)
    await WORKER_ENGINE.set_pause_new_risk(active)
    return {"status": "ok"}

@app.post("/recovery-only")
async def recovery_only(req: dict):
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    active = req.get("active", True)
    await WORKER_ENGINE.set_recovery_only(active)
    return {"status": "ok"}

@app.post("/kill-switch")
async def kill_switch(req: dict):
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    active = req.get("active", True)
    await WORKER_ENGINE.set_kill_switch(active)
    return {"status": "ok"}

@app.post("/reconcile")
async def reconcile():
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    res = await WORKER_ENGINE.trigger_reconciliation()
    return {"status": res}
