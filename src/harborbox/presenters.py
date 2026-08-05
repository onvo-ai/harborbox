from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from harborbox.models import Execution
from harborbox.schemas import ExecutionError, ExecutionResponse, ExecutionResult, LogOutput


def elapsed_ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


async def execution_response(
    session: AsyncSession,
    execution: Execution,
    *,
    waiting_for: str | None = None,
) -> ExecutionResponse:
    queue_position: int | None = None
    if execution.status == "queued":
        position_query = select(func.count()).select_from(Execution).where(
            Execution.status == "queued",
            Execution.created_at <= execution.created_at,
        )
        queue_position = int((await session.scalar(position_query)) or 0)

    result: dict[str, Any] = execution.result or {}
    return ExecutionResponse(
        id=execution.id,
        sandbox_id=execution.sandbox_id,
        kind=execution.kind,  # type: ignore[arg-type]
        status=execution.status,  # type: ignore[arg-type]
        queue_position=queue_position,
        waiting_for=waiting_for,  # type: ignore[arg-type]
        logs=LogOutput.model_validate(result.get("logs", {}))
        if execution.result
        else None,
        results=[
            ExecutionResult.model_validate(item) for item in result.get("results", [])
        ],
        error=ExecutionError.model_validate(execution.error)
        if execution.error
        else None,
        exit_code=result.get("exit_code"),
        created_at=execution.created_at,
        admitted_at=execution.admitted_at,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        queued_ms=elapsed_ms(
            execution.created_at,
            execution.admitted_at or execution.finished_at,
        ),
        startup_ms=elapsed_ms(execution.admitted_at, execution.started_at),
        execution_ms=elapsed_ms(execution.started_at, execution.finished_at),
    )
