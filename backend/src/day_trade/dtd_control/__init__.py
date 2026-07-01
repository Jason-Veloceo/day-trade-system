"""Process control for the DTD live observer (scripts/dtd_run.py).

Allows the backend (and the engine UI) to start/stop/status-check the
DTD ingestion observer without the operator having to manage a
separate terminal. The observer is launched as a detached subprocess
so it survives `uvicorn --reload` cycles.
"""

from day_trade.dtd_control.controller import (
    DtdObserverController,
    DtdObserverStatus,
    get_controller,
)

__all__ = [
    "DtdObserverController",
    "DtdObserverStatus",
    "get_controller",
]
