"""待推送分析数据集与用户洞察共享快照。

用户洞察页和订阅推送读的是同一份快照对象：快照按版本缓存，洞察报告新增或状态变化时
统一失效，下一次读取再重建。这样订阅推送消息里引用的洞察内容不会和用户洞察页展示的
报告各算一份。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

SNAPSHOT_NAME = "userinsight_snapshot"

CONTENT_DATASETS: dict[str, str] = {
    "日报": "daily",
    "告警摘要": "alarm",
    "洞察快报": "insight",
}
DATASET_NAMES = {"daily": "日报", "alarm": "告警摘要", "insight": "洞察快报"}

IMPORTANT_ALARM_LEVELS = {"P0", "P1", "紧急", "严重", "重要"}
PENDING_ALARM_STATUSES = {"已触发", "处理中"}


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _published_reports() -> list[dict[str, Any]]:
    return [row for row in store.rows("userinsight") if row.get("status") == "已发布"]


def build_userinsight_snapshot() -> dict[str, Any]:
    """重建洞察报告快照；调用方负责放在 store.lock() 里。"""
    reports = _published_reports()
    items = [
        {
            "报告编号": row.get("报告编号"),
            "分析主题": row.get("分析主题"),
            "目标人群": row.get("目标人群"),
            "关键指标": row.get("关键指标"),
            "核心发现": row.get("核心发现"),
            "数据区间": row.get("数据区间"),
            "生成时间": row.get("生成时间"),
        }
        for row in reports
    ]
    version = store.next_snapshot_version(SNAPSHOT_NAME)
    snapshot = {
        "名称": SNAPSHOT_NAME,
        "版本": version,
        "生成时间": _now_text(),
        "报告总数": len(store.rows("userinsight")),
        "已发布数": len(reports),
        "条目": items,
    }
    return store.save_snapshot(SNAPSHOT_NAME, snapshot)


def get_userinsight_snapshot() -> dict[str, Any]:
    """读取共享快照；缺失时只重建一次，并发读取拿到的仍是同一份。"""
    with store.lock():
        snapshot = store.cached_snapshot(SNAPSHOT_NAME)
        if snapshot is None:
            snapshot = build_userinsight_snapshot()
        return snapshot


def invalidate_userinsight_snapshot() -> None:
    """洞察报告数据变化时失效快照，避免订阅推送引用过期内容。"""
    with store.lock():
        store.invalidate_snapshot(SNAPSHOT_NAME)


def daily_dataset() -> dict[str, Any]:
    metrics = store.rows("realtimemetric")
    online = [row for row in metrics if row.get("status") == "已上线"]
    alarms = store.rows("alarmhistory")
    pending_alarms = [row for row in alarms if _is_important_alarm(row)]
    reports = _published_reports()
    headline = (
        f"已上线指标 {len(online)} 项，待处置重要告警 {len(pending_alarms)} 条，"
        f"已发布洞察报告 {len(reports)} 篇"
    )
    items = [
        {
            "标题": "指标运行概况",
            "摘要": headline,
            "来源": "实时指标",
            "时间": _now_text(),
            "重要性": "普通",
        },
        {
            "标题": "待处置告警",
            "摘要": "、".join(
                f"{row.get('记录编号')}（{row.get('告警规则')}）" for row in pending_alarms[:3]
            ) or "当前没有待处置告警",
            "来源": "告警历史",
            "时间": _now_text(),
            "重要性": "重要" if pending_alarms else "普通",
        },
    ]
    return {
        "key": "daily",
        "名称": DATASET_NAMES["daily"],
        "摘要": headline,
        "条目数": len(items),
        "重要条目数": sum(1 for item in items if item["重要性"] == "重要"),
        "条目": items,
    }


def _alarm_level(row: dict[str, Any]) -> str:
    level = str(row.get("告警等级") or "").strip()
    if level:
        return level
    return "P0" if row.get("abnormal") else "P2"


def _is_important_alarm(row: dict[str, Any]) -> bool:
    if row.get("abnormal"):
        return True
    if str(row.get("处置状态") or "") in PENDING_ALARM_STATUSES:
        return True
    if str(row.get("status") or "") in PENDING_ALARM_STATUSES:
        return True
    return _alarm_level(row) in IMPORTANT_ALARM_LEVELS


def alarm_dataset() -> dict[str, Any]:
    alarms = store.rows("alarmhistory")
    important = [row for row in alarms if _is_important_alarm(row)]
    important.sort(key=lambda row: str(row.get("触发时间") or ""), reverse=True)
    items = [
        {
            "标题": f"{row.get('记录编号')} {row.get('告警规则')}",
            "摘要": (
                f"等级 {_alarm_level(row)}，当前值 {row.get('当前值')}，"
                f"阈值对比 {row.get('阈值对比')}，处置状态 {row.get('处置状态') or row.get('status')}"
            ),
            "来源": "告警历史",
            "时间": row.get("触发时间"),
            "重要性": "重要" if _is_important_alarm(row) else "普通",
        }
        for row in important[:5]
    ]
    headline = f"重要告警 {len(important)} 条，共 {len(alarms)} 条告警记录"
    return {
        "key": "alarm",
        "名称": DATASET_NAMES["alarm"],
        "摘要": headline,
        "条目数": len(items),
        "重要条目数": sum(1 for item in items if item["重要性"] == "重要"),
        "条目": items,
    }


def insight_dataset(snapshot: dict[str, Any]) -> dict[str, Any]:
    items = [
        {
            "标题": f"{item['报告编号']} {item['分析主题']}",
            "摘要": (
                f"目标人群 {item['目标人群']}，关键指标 {item['关键指标']}，"
                f"核心发现 {item['核心发现']}"
            ),
            "来源": "用户洞察",
            "时间": item.get("生成时间"),
            "重要性": "普通",
        }
        for item in snapshot["条目"][:5]
    ]
    headline = f"已发布洞察报告 {snapshot['已发布数']} 篇（快照版本 v{snapshot['版本']}）"
    return {
        "key": "insight",
        "名称": DATASET_NAMES["insight"],
        "摘要": headline,
        "条目数": len(items),
        "重要条目数": 0,
        "条目": items,
        "快照版本": snapshot["版本"],
    }


def build_push_datasets() -> dict[str, Any]:
    """一次读取三份待推送分析数据集；洞察部分直接复用共享快照。"""
    snapshot = get_userinsight_snapshot()
    datasets = [daily_dataset(), alarm_dataset(), insight_dataset(snapshot)]
    return {
        "snapshot_version": snapshot["版本"],
        "snapshot_generated_at": snapshot["生成时间"],
        "datasets": datasets,
    }


def dataset_by_content(content: str, datasets: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    key = CONTENT_DATASETS.get(str(content or "").strip())
    return datasets.get(key) if key else None
