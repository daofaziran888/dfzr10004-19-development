"""保留策略试算回归：以 STOR-0002 与 AUDI-0002 为主线，验证容量估算、
待归档记录与启用资格同步变化，历史已归档记录保持可查。

运行：cd backend && PYTHONPATH=. .venv/bin/python tests/test_retention_regression.py
"""
from __future__ import annotations

import threading
from datetime import date, timedelta

from app.services.audit import AuditService
from app.services.retention import retention_service
from app.services.storage import StorageService
from app.store import store

storage = StorageService()
audit = AuditService()

TODAY = date.today()
PASS = "PASS"


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"[FAIL] {label} {detail}")
    print(f"[{PASS}] {label}")


def stor2() -> dict:
    return store.find("storage", 2)


def audi(entry_id: int) -> dict:
    return store.find("audit", entry_id)


def main() -> None:
    # ---- 1. 未试算不得启用 ----
    entry, message = storage.run_action(2, "启用配置")
    check("未试算时启用被拦截", entry is None and "试算" in message, message)

    # ---- 2. 首次试算：容量/分片/过期区间可算，待复核记录阻断 ----
    trial, message = retention_service.run_trial(2, {"试算天数": 30, "日增数据量MB": 512})
    assert trial is not None, message
    check("试算分片数=每日2片×30天", trial["分片数"] == 60, str(trial["分片数"]))
    check("试算预计容量=15360×1.0×0.30", trial["预计容量MB"] == 4608.0, str(trial["预计容量MB"]))
    expect_window = [(TODAY + timedelta(days=90)).isoformat(), (TODAY + timedelta(days=119)).isoformat()]
    check("过期区间=今天+90天起30天", trial["过期区间"] == expect_window, str(trial["过期区间"]))
    affected_codes = {item["记录编号"] for item in trial["受影响记录"]}
    check("受影响记录含 AUDI-0001/0002 不含已归档 0003", affected_codes == {"AUDI-0001", "AUDI-0002"}, str(affected_codes))
    check("待复核记录阻断启用资格", trial["可启用"] is False and len(trial["阻断记录"]) == 1)

    entry, message = storage.run_action(2, "启用配置")
    check("试算未确认时启用被拦截", entry is None and "确认" in message, message)
    _, message = retention_service.run_action(trial["id"], "确认试算")
    check("未通过试算不得确认", "未通过" in message, message)

    # ---- 3. 复核阻断记录后重算：同配置只留最新，清单同步进审计台账 ----
    _, message = audit.run_action(1, "标记复核")
    check("AUDI-0001 标记复核", audi(1)["status"] == "已复核", message)

    trial2, message = retention_service.run_trial(2, {"试算天数": 30, "日增数据量MB": 512})
    assert trial2 is not None, message
    remaining = [row for row in store.rows("retention") if row["配置编号"] == "STOR-0002"]
    check("重复试算只保留最新结果", len(remaining) == 1 and remaining[0]["试算编号"] == trial2["试算编号"])
    check("复核后试算通过", trial2["可启用"] is True)
    check("AUDI-0001/0002 进入待归档", audi(1)["status"] == "待归档" and audi(2)["status"] == "待归档")
    check("待归档清单含两条记录", len(trial2["待归档清单"]["记录"]) == 2)
    lists = retention_service.pending_lists()
    check("清单台账可汇总查询", any(item["试算编号"] == trial2["试算编号"] for item in lists))

    # ---- 4. 确认后启用；启用资格与试算同步 ----
    _, message = retention_service.run_action(trial2["id"], "确认试算")
    entry, message = storage.run_action(2, "启用配置")
    check("确认试算后允许启用", entry is not None and stor2()["status"] == "已启用", message)

    # ---- 5. 导入上次配置：先差异后确认；变更后资格/容量/清单同步失效并重算 ----
    policy = {"配置编号": "STOR-0002", "存储路径": "/data/collect/audit", "文件格式": "JSON",
              "分片大小": "256MB", "压缩算法": "gzip", "保留天数": "60", "归档策略": "到期归档"}
    result = storage.import_policy(policy, confirm=False)
    check("导入先展示差异", result["needs_confirm"] is True and {d["字段"] for d in result["diff"]} == {"压缩算法", "保留天数"}, str(result.get("diff")))
    check("未确认前配置不变", stor2()["保留天数"] == "90")
    result = storage.import_policy(policy, confirm=True)
    check("确认后差异落库", result["ok"] and stor2()["保留天数"] == "60" and stor2()["压缩算法"] == "gzip")
    check("已启用配置变更后退回待校验", stor2()["status"] == "待校验")

    entry, message = storage.run_action(2, "启用配置")
    check("配置变更后旧试算失效", entry is None and "重新试算" in message, message)

    trial3, message = retention_service.run_trial(2, {"试算天数": 30, "日增数据量MB": 512})
    assert trial3 is not None, message
    check("容量估算同步变化(0.30->0.35)", trial3["预计容量MB"] == 5376.0, str(trial3["预计容量MB"]))
    expect_window3 = [(TODAY + timedelta(days=60)).isoformat(), (TODAY + timedelta(days=89)).isoformat()]
    check("过期区间同步变化", trial3["过期区间"] == expect_window3, str(trial3["过期区间"]))
    check("待归档记录同步保持", audi(1)["status"] == "待归档" and audi(2)["status"] == "待归档"
          and len(trial3["待归档清单"]["记录"]) == 2)

    # ---- 6. 并发争用同一归档窗口只放行一个 ----
    for code, days in (("STOR-0010", 10), ("STOR-0011", 20)):
        created, missing = storage.create_entry({"配置编号": code, "存储路径": f"/data/{code}", "文件格式": "JSON",
                                                 "分片大小": "128MB", "压缩算法": "gzip", "保留天数": str(days), "归档策略": "到期归档"})
        assert not missing, missing
    results: dict[str, str] = {}
    barrier = threading.Barrier(2)

    def submit(config_id: int, tag: str) -> None:
        barrier.wait()
        _, msg = retention_service.run_trial(config_id, {"试算天数": 30})
        results[tag] = msg

    t1 = threading.Thread(target=submit, args=(4, "STOR-0010"))
    t2 = threading.Thread(target=submit, args=(5, "STOR-0011"))
    t1.start(); t2.start(); t1.join(); t2.join()
    ok_count = sum(1 for msg in results.values() if msg.startswith("试算完成"))
    conflict_count = sum(1 for msg in results.values() if "争用" in msg)
    check("并发争用只放行一个占用", ok_count == 1 and conflict_count == 1, str(results))

    # ---- 7. 试算结果上传导入：以文件参数重算并替换 ----
    doc = retention_service.export_trial(trial3["id"])
    assert doc is not None
    trial4, message = retention_service.import_trial(doc)
    check("上传试算结果重新登记", trial4 is not None and trial4["来源"] == "上传导入", message)
    remaining = [row for row in store.rows("retention") if row["配置编号"] == "STOR-0002"]
    check("导入后仍只保留最新结果", len(remaining) == 1 and remaining[0]["试算编号"] == trial4["试算编号"])

    # ---- 8. 审计清单导入：去重与缺字段跳过 ----
    report = audit.import_entries([
        {"记录编号": "AUDI-0002", "操作类型": "重复", "采集计划": "重复"},
        {"记录编号": "AUDI-0100", "操作类型": "导入", "采集计划": "计划X", "执行时间": TODAY.isoformat()},
        {"操作类型": "缺编号", "采集计划": "计划Y"},
    ])
    check("审计清单导入新增1跳过2", len(report["created"]) == 1 and len(report["skipped"]) == 2, str(report["skipped"]))

    # ---- 9. 归档动作与历史可查 ----
    _, message = audit.run_action(2, "归档记录")
    check("AUDI-0002 待归档后归档", audi(2)["status"] == "已归档", message)
    archived = [row for row in store.rows("audit") if row["status"] == "已归档"]
    check("历史已归档记录保持可查", {row["记录编号"] for row in archived} >= {"AUDI-0002", "AUDI-0003"})
    check("已归档记录不再进入试算范围", all(item["记录编号"] != "AUDI-0003" for row in store.rows("retention") for item in row["受影响记录"]))

    print("\n全部回归用例通过")


if __name__ == "__main__":
    main()
