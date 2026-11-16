"""订阅推送业务规则：状态流转、字段校验、排期口径都收在这里。

排期相关的两个约定：
- 启用订阅时若没有可用的下次推送时间，按当前时刻落到下一个排期锚点；
- 暂停订阅时把下次推送时间整体顺延一个完整频率周期，恢复后不会立刻补发
  暂停期间积压的内容。每次排期变化都记进「排期变更记录」，便于追溯。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services import insights
from app.services.schedule import (
    ScheduleError,
    advance_to_next_anchor,
    format_datetime,
    postpone_after_pause,
    parse_datetime,
    parse_frequency,
    parse_quiet_window,
)
from app.store import store

MODULE = "subscription"
REQUIRED_FIELDS = ["订阅编号", "订阅内容", "接收人"]
ALL_FIELDS = ["订阅编号", "订阅内容", "接收人", "推送渠道", "推送频率", "数据粒度", "静默时段", "下次推送时间"]
STATUS_ORDER = ["草稿", "订阅评审中", "已启用", "已暂停"]
ACTION_RULES = {"提交订阅评审": "订阅评审中", "启用订阅": "已启用", "暂停订阅": "已暂停"}
NEGATIVE_ACTIONS = []


class SubscriptionService:
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
            rows = [row for row in rows if keyword in str(row.get("订阅编号", ""))]
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
        content = str(values.get("订阅内容") or "").strip()
        if content not in insights.CONTENT_DATASETS:
            return None, [f"订阅内容需为：{'、'.join(insights.CONTENT_DATASETS)}"]

        with store.lock():
            frequency = str(values.get("推送频率") or "每日").strip()
            try:
                parse_frequency(frequency)
                parse_quiet_window(values.get("静默时段"))
            except ScheduleError as error:
                return None, [str(error)]

            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in ALL_FIELDS})
            entry["订阅内容"] = content
            entry["推送频率"] = frequency
            entry["推送渠道"] = str(values.get("推送渠道") or "邮件").strip()
            entry["数据粒度"] = str(values.get("数据粒度") or "").strip()
            entry["静默时段"] = str(values.get("静默时段") or "").strip()
            scheduled = parse_datetime(values.get("下次推送时间"))
            if scheduled is None:
                scheduled = advance_to_next_anchor(datetime.now(), frequency, datetime.now())
            entry["下次推送时间"] = format_datetime(scheduled)
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["排期变更记录"] = [{
                "时间": format_datetime(datetime.now()),
                "变更": "登记订阅",
                "原下次推送时间": "",
                "新下次推送时间": entry["下次推送时间"],
            }]
            rows.append(entry)
            return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"订阅配置 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于订阅推送可执行范围"
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"

            previous = str(entry.get("下次推送时间") or "")
            try:
                if action == "启用订阅":
                    self._schedule_on_enable(entry)
                elif action == "暂停订阅":
                    self._schedule_on_pause(entry)
            except ScheduleError as error:
                return None, str(error)

            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            if str(entry.get("下次推送时间") or "") != previous:
                entry.setdefault("排期变更记录", []).append({
                    "时间": format_datetime(datetime.now()),
                    "变更": action,
                    "原下次推送时间": previous,
                    "新下次推送时间": entry.get("下次推送时间") or "",
                    "排期依据": entry.pop("_排期依据", ""),
                })
            else:
                entry.pop("_排期依据", None)
            return entry, f"订阅配置已{action}"

    @staticmethod
    def _schedule_on_enable(entry: dict[str, Any]) -> None:
        frequency = str(entry.get("推送频率") or "")
        parse_frequency(frequency)
        now = datetime.now()
        scheduled = parse_datetime(entry.get("下次推送时间"))
        if scheduled is not None and scheduled > now:
            return
        # 暂停过又恢复时，下次推送已被顺延；只有过期或缺失才重排。
        entry["下次推送时间"] = format_datetime(advance_to_next_anchor(now, frequency, now))
        entry["_排期依据"] = "启用时按当前时刻落到下一个排期锚点"

    @staticmethod
    def _schedule_on_pause(entry: dict[str, Any]) -> None:
        frequency = str(entry.get("推送频率") or "")
        parse_frequency(frequency)
        now = datetime.now()
        scheduled = parse_datetime(entry.get("下次推送时间")) or now
        entry["下次推送时间"] = format_datetime(postpone_after_pause(scheduled, frequency, now))
        entry["_排期依据"] = "暂停时按原计划与实际暂停时间中较晚者顺延一个完整周期"
