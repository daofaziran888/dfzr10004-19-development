"""采集审计日志业务规则：状态流转、字段校验与筛选口径都收在这里。

状态序列：已记录 -> 已复核 -> 待归档 -> 已归档。
「待归档」由保留策略试算通过后写入（见 services/retention.py），
已归档记录保持可查，不再参与试算清理范围。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "audit"
REQUIRED_FIELDS = ["记录编号", "操作类型", "采集计划"]
STATUS_ORDER = ["已记录", "已复核", "待归档", "已归档"]
ACTION_RULES = {"标记复核": "已复核", "归档记录": "已归档"}
NEGATIVE_ACTIONS: list[str] = []


class AuditService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in ("执行人", "执行时间", "结果摘要", "变更内容", "复核状态"):
            if values.get(field) is not None:
                entry[field] = values.get(field)
        status = str(values.get("status") or "").strip()
        entry["status"] = status if status in STATUS_ORDER else STATUS_ORDER[0]
        entry["pending"] = entry["status"] != STATUS_ORDER[-1]
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"审计记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集审计日志可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"审计记录已{action}"

    def import_entries(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """从导入文件读取审计清单：按记录编号去重，缺字段的条目跳过并说明。"""
        created: list[dict[str, Any]] = []
        skipped: list[str] = []
        existing_codes = {str(row.get("记录编号") or "") for row in store.rows(MODULE)}
        for item in items:
            if not isinstance(item, dict):
                skipped.append("非对象条目")
                continue
            code = str(item.get("记录编号") or "").strip()
            if code and code in existing_codes:
                skipped.append(f"{code}（记录编号已存在）")
                continue
            entry, missing = self.create_entry(item)
            if missing:
                skipped.append(f"{code or '未编号'}（缺少：{'、'.join(missing)}）")
                continue
            existing_codes.add(str(entry.get("记录编号") or ""))
            created.append(entry)
        return {
            "ok": True,
            "message": f"审计清单导入完成：新增 {len(created)} 条，跳过 {len(skipped)} 条",
            "created": created,
            "skipped": skipped,
        }
