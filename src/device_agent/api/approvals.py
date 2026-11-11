from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from device_agent.container import Container, get_container
from device_agent.schemas import ApprovalDecision, ApprovalOut

router = APIRouter(prefix="/api/approvals", tags=["工具审批"])
Deps = Annotated[Container, Depends(get_container)]

Status = Literal["pending", "approved", "executed", "failed", "rejected", "expired"]


@router.get("", summary="审批列表（自动任务中需要人工确认的设备工具调用）")
async def list_approvals(
    c: Deps,
    status: Status | None = None,
    device_id: str | None = None,
    context_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[ApprovalOut]:
    approvals = await c.approvals.list_approvals(
        status=status, device_id=device_id, context_id=context_id, limit=limit
    )
    return [ApprovalOut.model_validate(a) for a in approvals]


@router.get("/{approval_id}", summary="审批详情")
async def get_approval(approval_id: str, c: Deps) -> ApprovalOut:
    return ApprovalOut.model_validate(await c.approvals.get(approval_id))


@router.post("/{approval_id}/approve", summary="批准：按登记时的原始参数执行该工具调用")
async def approve(approval_id: str, c: Deps, body: ApprovalDecision | None = None) -> ApprovalOut:
    return ApprovalOut.model_validate(await c.approvals.approve(approval_id, body.note if body else None))


@router.post("/{approval_id}/reject", summary="拒绝")
async def reject(approval_id: str, c: Deps, body: ApprovalDecision | None = None) -> ApprovalOut:
    return ApprovalOut.model_validate(await c.approvals.reject(approval_id, body.note if body else None))
