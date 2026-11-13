"""保留策略试算业务规则：顺着存储配置到审计记录的处理环节，把容量估算、
过期区间、待归档清单与启用资格收在这一处，保证几处口径同步变化。

口径说明（过期边界与压缩估算方式）：
- 过期边界：审计记录/分片的到期日 = 写入日期 + 保留天数，到期当日 00:00 进入清理窗口；
  试算窗口为未来 N 天 [今天, 今天+N]，分片过期区间（即归档窗口）为
  [今天+保留天数, 今天+保留天数+N-1]。
- 压缩估算：预计容量 = 原始容量 × 文件格式系数 × 压缩率；分片数按天切分，
  每天至少 1 个分片，日写入量超过分片大小则按 ceil(日写入/分片大小) 计。
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
from datetime import date, timedelta
from typing import Any

from app.store import store

MODULE = "retention"
AUDIT_MODULE = "audit"
STORAGE_MODULE = "storage"

TRIAL_STATUS_ORDER = ["已生成", "已确认", "已归档"]
TRIAL_ACTION_RULES = {"确认试算": "已确认", "归档结果": "已归档"}

# 压缩估算口径：算法 -> 压缩率（越小省得越多）；文件格式 -> 体积系数
COMPRESSION_RATIOS = {
    "gzip": 0.35,
    "zstd": 0.30,
    "snappy": 0.50,
    "lz4": 0.45,
    "不压缩": 1.0,
    "none": 1.0,
}
FORMAT_FACTORS = {
    "json": 1.0,
    "csv": 0.80,
    "parquet": 0.60,
    "avro": 0.65,
    "orc": 0.60,
}

DEFAULT_SHARD_SIZE_MB = 128
DEFAULT_RETENTION_DAYS = 30
DEFAULT_HORIZON_DAYS = 30
DEFAULT_DAILY_VOLUME_MB = 512

CONFIG_FIELDS = ["存储路径", "文件格式", "分片大小", "压缩算法", "保留天数", "归档策略"]

# 试算提交是「检查窗口占用 + 替换同配置旧试算 + 写台账」的组合操作，
# 用一把锁保证两个并发试算争用同一归档窗口时只放行一个。
_trial_lock = threading.Lock()


def _parse_int(value: Any) -> int | None:
    """从「90」「90天」这类写法里抠出整数；抠不出来返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else None


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        match = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                return None
    return None


def _lookup(table: dict[str, float], raw: Any) -> tuple[float, str | None]:
    text = str(raw or "").strip().lower()
    for key, factor in table.items():
        if key.lower() in text:
            return factor, None
    return 1.0, str(raw or "")


def config_fingerprint(config: dict[str, Any]) -> str:
    """配置指纹：试算所依据的六个字段任一变化，指纹即变，启用资格随之失效。"""
    joined = "|".join(str(config.get(field) or "").strip() for field in CONFIG_FIELDS)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


class RetentionService:
    # ---------- 查询 ----------
    def list_trials(self, config_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(MODULE)
        if config_code:
            rows = [row for row in rows if row.get("配置编号") == config_code]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))

    def get_trial(self, trial_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, trial_id)

    def latest_trial(self, config_code: str) -> dict[str, Any] | None:
        trials = [row for row in store.rows(MODULE) if row.get("配置编号") == config_code]
        return trials[-1] if trials else None

    def pending_lists(self) -> list[dict[str, Any]]:
        """待归档清单台账：所有在效试算（未归档）生成的清单汇总，供审计清单页同步展示。"""
        lists: list[dict[str, Any]] = []
        for trial in store.rows(MODULE):
            if trial.get("status") == "已归档":
                continue
            pending = trial.get("待归档清单")
            if pending and pending.get("记录"):
                lists.append(pending)
        return lists

    # ---------- 试算 ----------
    def run_trial(self, config_id: int, overrides: dict[str, Any] | None = None, source: str = "页面试算") -> tuple[dict[str, Any] | None, str]:
        config = store.find(STORAGE_MODULE, config_id)
        if config is None:
            return None, f"存储配置 {config_id} 不存在或已归档"
        overrides = overrides or {}
        with _trial_lock:
            return self._run_trial_locked(config, overrides, source)

    def _run_trial_locked(self, config: dict[str, Any], overrides: dict[str, Any], source: str) -> tuple[dict[str, Any] | None, str]:
        notes: list[str] = []
        today = date.today()

        shard_size = _parse_int(config.get("分片大小"))
        if shard_size is None or shard_size <= 0:
            shard_size = DEFAULT_SHARD_SIZE_MB
            notes.append(f"分片大小未识别，按默认 {DEFAULT_SHARD_SIZE_MB}MB 估算")
        retention_days = _parse_int(config.get("保留天数"))
        if retention_days is None or retention_days <= 0:
            retention_days = DEFAULT_RETENTION_DAYS
            notes.append(f"保留天数未识别，按默认 {DEFAULT_RETENTION_DAYS} 天估算")
        horizon = _parse_int(overrides.get("试算天数")) or DEFAULT_HORIZON_DAYS
        horizon = max(1, min(horizon, 365))
        daily_volume = _parse_int(overrides.get("日增数据量MB")) or DEFAULT_DAILY_VOLUME_MB
        daily_volume = max(1, daily_volume)

        ratio, bad_algo = _lookup(COMPRESSION_RATIOS, config.get("压缩算法"))
        if bad_algo is not None:
            notes.append(f"压缩算法「{bad_algo}」未收录，按不压缩（系数 1.0）估算")
        format_factor, bad_format = _lookup(FORMAT_FACTORS, config.get("文件格式"))
        if bad_format is not None:
            notes.append(f"文件格式「{bad_format}」未收录，按系数 1.0 估算")

        # 分片与容量
        shards_per_day = max(1, math.ceil(daily_volume / shard_size))
        shard_count = shards_per_day * horizon
        raw_capacity_mb = round(daily_volume * horizon, 2)
        estimated_capacity_mb = round(raw_capacity_mb * format_factor * ratio, 2)

        # 过期区间（归档窗口）：试算窗口内写出的分片，陆续在 [今天+保留, 今天+保留+N-1] 到期
        window_start = today + timedelta(days=retention_days)
        window_end = today + timedelta(days=retention_days + horizon - 1)

        # 归档窗口争用：别的配置的在效试算占用了重叠窗口时，只放行先占的那个
        config_code = str(config.get("配置编号"))
        conflict = self._window_conflict(config_code, window_start, window_end)
        if conflict is not None:
            return None, (
                f"归档窗口 {window_start.isoformat()}~{window_end.isoformat()} "
                f"与 {conflict['配置编号']} 的试算 {conflict['试算编号']} 争用，仅放行一个占用，本次试算未登记"
            )

        # 同一配置重复试算只保留最新结果：先回滚旧试算置出的待归档，再作废旧试算；
        # 回滚发生在扫描审计记录之前，新的受影响清单按回滚后的状态重新计算
        replaced = self._replace_existing(config_code)

        # 受影响审计记录：到期日落在试算窗口内（含已逾期未清理）的未归档记录
        affected: list[dict[str, Any]] = []
        horizon_end = today + timedelta(days=horizon)
        for row in store.rows(AUDIT_MODULE):
            if row.get("status") == "已归档":
                continue
            written = _parse_date(row.get("执行时间"))
            if written is None:
                written = today
                notes.append(f"审计记录 {row.get('记录编号')} 执行时间未识别，按今日估算到期日")
            expiry = written + timedelta(days=retention_days)
            if expiry <= horizon_end:
                affected.append({
                    "id": row.get("id"),
                    "记录编号": row.get("记录编号"),
                    "当前状态": row.get("status"),
                    "执行时间": str(row.get("执行时间") or ""),
                    "到期日": expiry.isoformat(),
                })

        blockers = [item for item in affected if item["当前状态"] == "已记录"]
        to_archive = [item for item in affected if item["当前状态"] == "已复核"]
        eligible = not blockers

        rows = store.rows(MODULE)
        trial_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        trial_no = f"TRIAL-{trial_id:04d}"

        pending_list: dict[str, Any] = {"清单编号": f"PEND-{trial_id:04d}", "试算编号": trial_no, "配置编号": config_code, "生成时间": today.isoformat(), "记录": []}
        if eligible:
            for item in to_archive:
                row = store.find(AUDIT_MODULE, int(item["id"]))
                if row is None or row.get("status") != "已复核":
                    continue
                row["status"] = "待归档"
                row["pending"] = True
                pending_list["记录"].append({**item, "原状态": "已复核"})

        trial = {
            "id": trial_id,
            "试算编号": trial_no,
            "配置编号": config_code,
            "存储路径": config.get("存储路径"),
            "文件格式": config.get("文件格式"),
            "分片大小MB": shard_size,
            "保留天数": retention_days,
            "归档策略": config.get("归档策略"),
            "压缩算法": config.get("压缩算法"),
            "试算参数": {"试算天数": horizon, "日增数据量MB": daily_volume},
            "试算窗口": [today.isoformat(), horizon_end.isoformat()],
            "归档窗口": [window_start.isoformat(), window_end.isoformat()],
            "分片数": shard_count,
            "每日分片数": shards_per_day,
            "原始容量MB": raw_capacity_mb,
            "压缩率": ratio,
            "格式系数": format_factor,
            "预计容量MB": estimated_capacity_mb,
            "过期区间": [window_start.isoformat(), window_end.isoformat()],
            "受影响记录": affected,
            "阻断记录": blockers,
            "待归档清单": pending_list,
            "可启用": eligible,
            "配置指纹": config_fingerprint(config),
            "来源": source,
            "估算说明": notes,
            "status": TRIAL_STATUS_ORDER[0],
            "pending": True,
            "abnormal": not eligible,
        }
        rows.append(trial)

        if replaced:
            notes.append(f"已替换同配置旧试算 {replaced}，仅保留最新结果")
        if blockers:
            notes.append("试算会清理仍被待复核记录引用的数据，该存储配置不得启用")
        message = f"试算完成：{trial_no}，分片 {shard_count} 个，预计容量 {estimated_capacity_mb}MB"
        if not eligible:
            message += f"；{len(blockers)} 条待复核记录阻断启用"
        return trial, message

    def _window_conflict(self, config_code: Any, start: date, end: date) -> dict[str, Any] | None:
        for trial in store.rows(MODULE):
            if trial.get("配置编号") == config_code or trial.get("status") == "已归档":
                continue
            window = trial.get("归档窗口") or []
            if len(window) != 2:
                continue
            other_start, other_end = _parse_date(window[0]), _parse_date(window[1])
            if other_start and other_end and start <= other_end and other_start <= end:
                return trial
        return None

    def _replace_existing(self, config_code: str) -> str | None:
        rows = store.rows(MODULE)
        replaced_no: str | None = None
        kept: list[dict[str, Any]] = []
        for trial in rows:
            if trial.get("配置编号") != config_code:
                kept.append(trial)
                continue
            replaced_no = str(trial.get("试算编号"))
            # 回滚旧试算置出的待归档：仍在待归档的按清单快照恢复原状态；已归档的不动
            for item in (trial.get("待归档清单") or {}).get("记录", []):
                row = store.find(AUDIT_MODULE, int(item.get("id") or 0))
                if row is not None and row.get("status") == "待归档":
                    row["status"] = item.get("原状态") or "已复核"
        rows[:] = kept
        return replaced_no

    # ---------- 试算动作 ----------
    def run_action(self, trial_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        trial = store.find(MODULE, trial_id)
        if trial is None:
            return None, f"试算 {trial_id} 不存在或已归档"
        if action not in TRIAL_ACTION_RULES:
            return None, f"动作「{action}」不属于保留策略试算可执行范围"
        target = TRIAL_ACTION_RULES[action]
        current = str(trial.get("status"))
        if current == "已归档":
            return None, "试算结果已归档，仅可查阅"
        if action == "确认试算":
            if current != "已生成":
                return None, "仅已生成的试算可确认"
            if not trial.get("可启用"):
                return None, "试算未通过：仍存在待复核记录引用的数据，不能确认"
        if action == "归档结果" and current == "已生成":
            return None, "试算尚未确认，不能直接归档分片结果"
        trial["status"] = target
        trial["pending"] = target != "已归档"
        return trial, f"试算结果{target}"

    # ---------- 启用资格 ----------
    def enable_block_reason(self, config: dict[str, Any]) -> str | None:
        """返回 None 表示允许启用；否则返回阻断原因。容量、待归档与启用资格共用同一份最新试算。"""
        trial = self.latest_trial(str(config.get("配置编号")))
        if trial is None:
            return "尚未完成保留策略试算，存储配置不得启用"
        if trial.get("status") == "已归档":
            return "最新试算结果已归档，请重新试算并确认后再启用"
        if trial.get("status") != "已确认":
            return "试算结果未确认，请先确认试算"
        if not trial.get("可启用"):
            return "试算会删除仍被待复核记录引用的数据，存储配置不得启用"
        if trial.get("配置指纹") != config_fingerprint(config):
            return "存储配置已变更，容量估算与待归档清单已失效，请重新试算"
        return None

    # ---------- 导入导出 ----------
    def export_trial(self, trial_id: int) -> dict[str, Any] | None:
        trial = store.find(MODULE, trial_id)
        if trial is None:
            return None
        return {"module": MODULE, "kind": "保留策略试算结果", "trial": trial}

    def import_trial(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """上传试算结果：以文件里的配置编号与试算参数为准重新试算，
        保证容量、待归档清单与当前台账一致，而不是直接信任文件里的数字。"""
        doc = payload.get("trial") if isinstance(payload.get("trial"), dict) else payload
        config_code = str(doc.get("配置编号") or "").strip()
        if not config_code:
            return None, "导入文件缺少配置编号，无法定位存储配置"
        config = next((row for row in store.rows(STORAGE_MODULE) if row.get("配置编号") == config_code), None)
        if config is None:
            return None, f"存储配置 {config_code} 不存在，试算结果导入失败"
        overrides = doc.get("试算参数") if isinstance(doc.get("试算参数"), dict) else {}
        return self.run_trial(int(config["id"]), overrides, source="上传导入")


retention_service = RetentionService()
