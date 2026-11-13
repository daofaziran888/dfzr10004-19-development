"""采集存储策略业务规则：状态流转、字段校验与筛选口径都收在这里。

启用配置前必须通过保留策略试算：最新试算已确认、可启用、且配置未再变更，
三者任一不满足都不得启用——容量估算、待归档清单与启用资格共用同一份试算结果。
"""
from __future__ import annotations

from typing import Any

from app.services.retention import CONFIG_FIELDS, retention_service
from app.store import store

MODULE = "storage"
REQUIRED_FIELDS = ["配置编号", "存储路径", "文件格式"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]


class StorageService:
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
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
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
        for field in CONFIG_FIELDS:
            if field not in entry and values.get(field) is not None:
                entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"存储配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集存储策略可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "启用配置":
            reason = retention_service.enable_block_reason(entry)
            if reason is not None:
                return None, reason
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        entry["是否启用"] = "是" if target == "已启用" else "否"
        return entry, f"存储配置已{action}"

    def import_policy(self, values: dict[str, Any], confirm: bool) -> dict[str, Any]:
        """保留策略包粘贴导入。

        配置编号不存在时直接登记为新配置；已存在时先返回逐字段差异，
        由调用方确认（confirm=True）后才落库，避免静默覆盖上次配置。
        """
        code = str(values.get("配置编号") or "").strip()
        if not code:
            return {"ok": False, "message": "策略包缺少配置编号，无法导入"}
        existing = next((row for row in store.rows(MODULE) if row.get("配置编号") == code), None)
        if existing is None:
            entry, missing = self.create_entry(values)
            if missing:
                return {"ok": False, "message": f"缺少必填字段：{'、'.join(missing)}"}
            return {"ok": True, "needs_confirm": False, "message": f"策略包已登记为新存储配置 {code}", "entry": entry}

        diff = []
        for field in [*CONFIG_FIELDS, "是否启用"]:
            if field in values and str(values.get(field) or "") != str(existing.get(field) or ""):
                diff.append({"字段": field, "原值": existing.get(field), "新值": values.get(field)})
        if not diff:
            return {"ok": True, "needs_confirm": False, "message": f"策略包与现有配置 {code} 一致，无需变更", "entry": existing}
        if not confirm:
            return {
                "ok": True,
                "needs_confirm": True,
                "message": f"配置 {code} 已存在，{len(diff)} 个字段有差异，请确认后再导入",
                "diff": diff,
            }
        for item in diff:
            existing[item["字段"]] = item["新值"]
        # 配置变更后旧试算指纹即失效；已启用的配置退回待校验，重新走校验与试算
        if existing.get("status") == "已启用":
            existing["status"] = "待校验"
            existing["pending"] = True
            existing["是否启用"] = "否"
        return {"ok": True, "needs_confirm": False, "message": f"策略包差异已确认，配置 {code} 已更新 {len(diff)} 个字段", "entry": existing, "diff": diff}
