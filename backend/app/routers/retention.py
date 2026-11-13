"""保留策略试算接口：按存储配置试算分片数、预计容量与过期区间，
通过后生成待归档清单并同步审计记录；同一配置只保留最新试算，
归档窗口争用时只放行一个占用。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas import ActionResult, EntryPayload
from app.services.retention import retention_service

router = APIRouter(prefix="/api/retention", tags=["保留策略试算"])


class TrialPayload(BaseModel):
    """提交试算：指定存储配置，可覆盖试算窗口与日增数据量。"""

    config_id: int
    试算天数: int | None = None
    日增数据量MB: int | None = None


class ImportPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


@router.get("/trials")
def list_trials(config_code: str | None = None) -> dict[str, Any]:
    """试算列表：同一配置只保留最新一次结果。"""
    items = retention_service.list_trials(config_code=config_code)
    return {"items": items, "total": len(items)}


@router.post("/trials", response_model=ActionResult)
def run_trial(payload: TrialPayload) -> ActionResult:
    """提交保留策略试算；归档窗口与他人在效试算争用时会被拒并说明占用方。"""
    overrides = {key: value for key, value in (("试算天数", payload.试算天数), ("日增数据量MB", payload.日增数据量MB)) if value is not None}
    trial, message = retention_service.run_trial(payload.config_id, overrides)
    if trial is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=trial)


@router.get("/trials/{trial_id}")
def get_trial(trial_id: int) -> dict[str, Any]:
    trial = retention_service.get_trial(trial_id)
    if trial is None:
        raise HTTPException(status_code=404, detail=f"试算 {trial_id} 不存在或已归档")
    return trial


@router.post("/trials/{trial_id}/actions", response_model=ActionResult)
def run_action(trial_id: int, payload: EntryPayload) -> ActionResult:
    """确认试算 / 归档结果；未通过或未确认的试算会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    trial, message = retention_service.run_action(trial_id, action)
    if trial is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=trial)


@router.get("/trials/{trial_id}/export")
def export_trial(trial_id: int) -> dict[str, Any]:
    """导出试算结果：下载后可再次上传导入。"""
    doc = retention_service.export_trial(trial_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"试算 {trial_id} 不存在或已归档")
    return doc


@router.post("/import", response_model=ActionResult)
def import_trial(payload: ImportPayload) -> ActionResult:
    """上传试算结果：以文件中的配置编号与试算参数重新试算，保证与台账一致。"""
    trial, message = retention_service.import_trial(payload.values)
    if trial is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=trial)


@router.get("/pending-list")
def pending_list() -> dict[str, Any]:
    """待归档清单台账：在效试算生成的清单汇总，审计清单页据此显示待归档记录。"""
    lists = retention_service.pending_lists()
    total = sum(len(item.get("记录", [])) for item in lists)
    return {"items": lists, "total": total}
