"""订阅推送合并与投递：把多频率订阅合并成批次，并落可追溯的推送记录。

几条口径（和前端时间线、概览里的字段一一对应）：

1. 合并维度是「接收人 + 推送渠道 + 推送频率 + 原定推送时间」。同一接收人在同一频率、
   同一排期上订阅了日报 / 告警摘要 / 洞察快报时，只生成一个批次、一条消息、一条记录，
   而不是按订阅内容各发一条。
2. 批次号由业务键确定性生成。整个「查重 → 建记录 → 推进状态 → 推进下次推送时间」都在
   store.lock() 里完成，所以并发到达同一批次只会落一条记录。
3. 状态只能沿处理阶段向前推进：待合并 → 已合并 → （静默暂存）→ 投递中 → 已投递 /
   投递失败 / 已合并投递。回退会被拒绝，重复推进同一阶段不会重复写轨迹。
4. 静默判定按「原定推送时间」和「实际投递时刻」共同计算（补推拖到静默里也不能直接发）。
   命中静默的批次进入静默暂存，静默结束后同一接收人、同一渠道的暂存批次合并成一次投递，
   避免静默结束瞬间集中轰炸；每个批次仍各有一条投递结果，非主批次通过「合并主批次」
   指回主投递，保持可追溯。
5. 重要告警优先：批次里含重要告警时，突破静默立即投递，标题与内容排序按告警口径；
   同批次的日报内容随批送达、不额外生成消息。这是「告警时效」与「静默保护」之间的
   一次真实权衡，裁决结果写在「优先级裁决」字段里。
6. 下次推送时间始终沿原排期锚点前进，不随实际投递时间漂移；暂停订阅时整体顺延一个周期。
"""
from __future__ import annotations

import copy
from datetime import datetime
from hashlib import sha1
from typing import Any, Iterable

from app.services import insights
from app.services.schedule import (
    ScheduleError,
    advance_to_next_anchor,
    format_datetime,
    in_quiet_hours,
    normalize_frequency,
    parse_datetime,
    parse_quiet_window,
    quiet_end,
    quiet_windows_text,
)
from app.store import store

MODULE = "subscription_push"

SUPPORTED_CHANNELS = {"邮件", "企业微信", "短信", "钉钉", "站内信"}
FREQUENCY_CODES = {"每日": "D", "每小时": "H", "每15分钟": "Q"}
CONTENT_ORDER = ["告警摘要", "日报", "洞察快报"]
PRIORITY_ORDER = {"重要告警": 0, "日报": 1, "普通": 2}

# 处理阶段与允许的前进方向；rank 只增不减，保证状态不会回退。
STAGE_RANK = {
    "待合并": 0,
    "已合并": 1,
    "静默暂存": 2,
    "投递中": 3,
    "已投递": 4,
    "已合并投递": 4,
    "投递失败": 4,
}
TERMINAL_STAGES = {"已投递", "已合并投递", "投递失败"}
DELIVERED_STAGES = {"已投递", "已合并投递"}


class PushError(ValueError):
    """推送参数或状态推进不合法。"""


def batch_code(recipient: str, channel: str, frequency: str, scheduled: datetime) -> str:
    """批次号：排期时间 + 频率 + 接收人/渠道摘要，同一业务批次恒定。"""
    digest = sha1(f"{recipient}|{channel}".encode("utf-8")).hexdigest()[:6].upper()
    return f"PUSH-{scheduled:%Y%m%d%H%M}-{FREQUENCY_CODES[frequency]}-{digest}"


def _minute(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


def _next_id() -> int:
    rows = store.rows(MODULE)
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _priority_of(contents: Iterable[str], alarm_dataset: dict[str, Any]) -> str:
    content_set = set(contents)
    if "告警摘要" in content_set and int(alarm_dataset.get("重要条目数") or 0) > 0:
        return "重要告警"
    if "日报" in content_set:
        return "日报"
    return "普通"


def _sorted_contents(contents: Iterable[str]) -> list[str]:
    unique = {str(item) for item in contents}
    return sorted(unique, key=lambda item: CONTENT_ORDER.index(item) if item in CONTENT_ORDER else 99)


class SubscriptionPushService:
    # ---- 查询 ----
    def list_records(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        recipient: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with store.lock():
            rows = copy.deepcopy(store.rows(MODULE))
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("批次号", "")) or keyword in str(row.get("消息标题", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if recipient:
            rows = [row for row in rows if row.get("接收人") == recipient]
        rows.sort(key=lambda row: (str(row.get("原定推送时间", "")), int(row.get("id", 0))), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_record(self, code: str) -> dict[str, Any] | None:
        with store.lock():
            row = store.find_by(MODULE, "批次号", code)
            return copy.deepcopy(row) if row else None

    def timeline(self, *, limit: int = 50) -> dict[str, Any]:
        """时间线 + 概览：每个推送批次一条，失败项单独汇总。"""
        bundle = insights.build_push_datasets()
        with store.lock():
            rows = copy.deepcopy(store.rows(MODULE))
        rows.sort(key=lambda row: (str(row.get("原定推送时间", "")), int(row.get("id", 0))), reverse=True)
        overview = {
            "总批次": len(rows),
            "已投递": sum(1 for row in rows if row.get("status") in DELIVERED_STAGES),
            "合并投递": sum(1 for row in rows if row.get("status") == "已合并投递"),
            "静默暂存": sum(1 for row in rows if row.get("status") == "静默暂存"),
            "投递失败": sum(1 for row in rows if row.get("status") == "投递失败"),
            "待投递": sum(1 for row in rows if row.get("status") in {"待合并", "已合并", "投递中"}),
            "重要告警突破静默": sum(
                1 for row in rows if row.get("静默判定") == "重要告警突破静默"
            ),
        }
        failures = [
            {
                "批次号": row.get("批次号"),
                "接收人": row.get("接收人"),
                "推送渠道": row.get("推送渠道"),
                "推送频率": row.get("推送频率"),
                "原定推送时间": row.get("原定推送时间"),
                "消息标题": row.get("消息标题"),
                "订阅内容": row.get("订阅内容"),
                "失败原因": row.get("失败原因") or "未记录失败原因",
                "静默判定": row.get("静默判定"),
            }
            for row in rows
            if row.get("status") == "投递失败"
        ]
        return {
            "generated_at": format_datetime(datetime.now()),
            "snapshot_version": bundle["snapshot_version"],
            "snapshot_generated_at": bundle["snapshot_generated_at"],
            "datasets": [
                {
                    "key": item["key"],
                    "名称": item["名称"],
                    "摘要": item["摘要"],
                    "条目数": item["条目数"],
                    "重要条目数": item["重要条目数"],
                }
                for item in bundle["datasets"]
            ],
            "overview": overview,
            "failures": failures,
            "batches": rows[: max(limit, 1)],
        }

    # ---- 执行推送 ----
    def dispatch(self, *, at: object = None, recipient: str | None = None) -> dict[str, Any]:
        moment = parse_datetime(at) or datetime.now()
        with store.lock():
            bundle = insights.build_push_datasets()
            datasets = {item["key"]: item for item in bundle["datasets"]}
            snapshot_version = bundle["snapshot_version"]

            groups, skipped = self._collect_groups(moment, recipient)
            created: list[dict[str, Any]] = []
            for scheduled, members in groups:
                record, is_new = self._dispatch_group(scheduled, members, moment, datasets, snapshot_version)
                if is_new:
                    created.append(record)
            # 统一在批次落库后释放：同一次静默结束（含补推时才发现静默已过期的批次）在这一步
            # 合并成一次投递，不会因为释放顺序不同拆成多条消息。
            released = self._release_held(moment, recipient)

            failures = sum(1 for record in created if record.get("status") == "投递失败")
            held = sum(1 for record in created if record.get("status") == "静默暂存")
            return {
                "at": format_datetime(moment),
                "generated": len(created),
                "delivered": sum(1 for record in created if record.get("status") in DELIVERED_STAGES),
                "held": held,
                "failed": failures,
                "released": released,
                "skipped": skipped,
                "batch_ids": [record.get("批次号") for record in created],
                "snapshot_version": snapshot_version,
            }

    def _collect_groups(
        self, moment: datetime, recipient: str | None
    ) -> tuple[list[tuple[datetime, list[dict[str, Any]]]], list[dict[str, str]]]:
        groups: dict[tuple[Any, ...], tuple[datetime, list[dict[str, Any]]]] = {}
        skipped: list[dict[str, str]] = []
        for row in store.rows("subscription"):
            if row.get("status") != "已启用":
                continue
            if recipient and row.get("接收人") != recipient:
                continue
            try:
                frequency = normalize_frequency(row.get("推送频率"))
                parse_quiet_window(row.get("静默时段"))
            except ScheduleError as error:
                skipped.append({"订阅编号": str(row.get("订阅编号")), "原因": str(error)})
                continue
            scheduled = parse_datetime(row.get("下次推送时间"))
            if scheduled is None:
                skipped.append({"订阅编号": str(row.get("订阅编号")), "原因": "下次推送时间缺失或格式不正确"})
                continue
            if scheduled > moment:
                continue
            if str(row.get("订阅内容") or "") not in insights.CONTENT_DATASETS:
                skipped.append({
                    "订阅编号": str(row.get("订阅编号")),
                    "原因": "订阅内容未接入待推送分析数据集",
                })
                continue
            key = (
                str(row.get("接收人") or ""),
                str(row.get("推送渠道") or ""),
                frequency,
                _minute(scheduled),
            )
            groups.setdefault(key, (scheduled, []))[1].append(row)
        ordered = sorted(groups.values(), key=lambda item: item[0])
        return ordered, skipped

    def _dispatch_group(
        self,
        scheduled: datetime,
        members: list[dict[str, Any]],
        moment: datetime,
        datasets: dict[str, dict[str, Any]],
        snapshot_version: int,
    ) -> tuple[dict[str, Any], bool]:
        recipient = str(members[0].get("接收人") or "")
        channel = str(members[0].get("推送渠道") or "")
        frequency = str(members[0].get("推送频率") or "")

        contents = _sorted_contents(str(member.get("订阅内容") or "") for member in members)
        alarm_dataset = datasets.get("alarm", {})
        priority = _priority_of(contents, alarm_dataset)
        # 静默判定同时看两个时刻：原定推送时间（排期本身落在静默里，静默结束后统一释放）
        # 和实际投递时刻（补推 / 补跑拖到静默里，也不能直接推送）。任一命中就算命中静默。
        judge_points = {scheduled, moment}
        quiet_hit = any(
            in_quiet_hours(point, member.get("静默时段"))
            for member in members
            for point in judge_points
        )
        quiet_ends = [
            end for member in members
            for point in judge_points
            if (end := quiet_end(point, member.get("静默时段"))) is not None
        ]
        quiet_until = max(quiet_ends) if quiet_ends else None
        quiet_text = quiet_windows_text(member.get("静默时段") for member in members)

        code = batch_code(recipient, channel, frequency, scheduled)
        existing = store.find_by(MODULE, "批次号", code)
        if existing is not None:
            # 并发或重复触发：直接返回已有记录，不再落第二条、不再投递。
            return existing, False

        entries = self._merge_entries(contents, datasets)
        title, summary, ruling, quiet_label = self._compose_message(
            recipient=recipient,
            scheduled=scheduled,
            frequency=frequency,
            contents=contents,
            entries=entries,
            datasets=datasets,
            priority=priority,
            quiet_hit=quiet_hit,
            quiet_text=quiet_text,
        )

        next_scheduled = min(
            advance_to_next_anchor(parse_datetime(member.get("下次推送时间")), frequency, moment)
            for member in members
        )
        for member in members:
            member["下次推送时间"] = format_datetime(next_scheduled)

        record: dict[str, Any] = {
            "id": _next_id(),
            "批次号": code,
            "status": "待合并",
            "pending": True,
            "abnormal": False,
            "接收人": recipient,
            "推送渠道": channel,
            "推送频率": frequency,
            "批次窗口": f"{scheduled:%Y-%m-%d %H:%M} / {frequency}",
            "订阅编号": "、".join(str(member.get("订阅编号") or "") for member in members),
            "订阅内容": "+".join(contents),
            "合并条数": len(entries),
            "消息标题": title,
            "消息摘要": summary,
            "消息条目": entries,
            "优先级": priority,
            "优先级裁决": ruling,
            "静默判定": quiet_label,
            "静默时段": quiet_text,
            "静默结束时间": format_datetime(quiet_until) if quiet_until else "",
            "原定推送时间": format_datetime(scheduled),
            "实际投递时间": "",
            "下次推送时间": format_datetime(next_scheduled),
            "投递结果": "待投递",
            "失败原因": "",
            "合并主批次": "",
            "合并批次列表": [],
            "洞察快照版本": snapshot_version,
            "分析数据集": "、".join(datasets[insights.CONTENT_DATASETS[content]]["名称"] for content in contents),
            "创建时间": format_datetime(moment),
            "处理轨迹": [{"阶段": "待合并", "时间": format_datetime(moment)}],
        }
        store.rows(MODULE).append(record)
        self._advance(record, "已合并", moment, 投递结果="已合并，等待投递")

        if channel not in SUPPORTED_CHANNELS:
            # 渠道不合法在合并后立刻判失败，不必等静默结束再报错。
            self._advance(
                record,
                "投递失败",
                moment,
                投递结果="投递失败",
                失败原因=f"推送渠道「{channel}」未接入",
                实际投递时间=format_datetime(moment),
            )
            return record, True

        if quiet_hit and priority != "重要告警":
            # 一律先进入暂存：即便处理时静默已经过去（补推/补跑），也交给本次的释放步骤
            # 与其它暂存批次一起合并投递，避免同一时刻出现第二条消息。
            self._advance(
                record,
                "静默暂存",
                moment,
                投递结果="静默暂存，待静默结束合并投递",
                静默判定="命中静默",
            )
            return record, True

        self._advance(record, "投递中", moment, 投递结果="投递中")
        self._advance(
            record,
            "已投递",
            moment,
            投递结果=self._delivery_result(priority, quiet_hit),
            静默判定=quiet_label,
            实际投递时间=format_datetime(moment),
        )
        return record, True

    def _merge_entries(
        self, contents: list[str], datasets: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for content in contents:
            dataset = datasets.get(insights.CONTENT_DATASETS[content])
            if dataset is None:
                continue
            for item in dataset["条目"]:
                identity = (str(item.get("来源")), str(item.get("标题")))
                if identity in seen:
                    continue
                seen.add(identity)
                entries.append({**item, "订阅内容": content})
        return entries

    def _compose_message(
        self,
        *,
        recipient: str,
        scheduled: datetime,
        frequency: str,
        contents: list[str],
        entries: list[dict[str, Any]],
        datasets: dict[str, dict[str, Any]],
        priority: str,
        quiet_hit: bool,
        quiet_text: str,
    ) -> tuple[str, str, str, str]:
        if priority == "重要告警":
            title = f"【重要告警】{recipient} {scheduled:%m-%d %H:%M} 合并推送"
            if quiet_hit:
                ruling = "重要告警优先于静默：突破静默立即投递，同批日报内容随批合并，本批次只投递一次"
            else:
                ruling = "重要告警优先于日报：标题与排序按告警口径，日报内容随批合并，本批次只投递一次"
        elif priority == "日报":
            title = f"【日报】{recipient} {scheduled:%m-%d %H:%M} 合并推送"
            ruling = "日报按频率合并：同接收人同频率只投递一次日报摘要"
        else:
            title = f"【订阅推送】{recipient} {scheduled:%m-%d %H:%M} 合并推送"
            ruling = "普通内容按频率合并投递"

        if quiet_hit and priority != "重要告警":
            ruling = f"静默优先：推迟到静默结束合并投递，静默时段 {quiet_text or '未设置'}"

        parts = []
        for content in contents:
            dataset = datasets.get(insights.CONTENT_DATASETS[content])
            if dataset:
                parts.append(f"{dataset['名称']}：{dataset['摘要']}")
        summary = "；".join(parts)
        if len(entries) > 1:
            summary = f"{summary}（合并 {len(entries)} 条内容）"
        quiet_label = "未命中静默"
        if quiet_hit:
            quiet_label = "重要告警突破静默" if priority == "重要告警" else "命中静默"
        return title, summary, ruling, quiet_label

    @staticmethod
    def _delivery_result(priority: str, quiet_hit: bool) -> str:
        if priority == "重要告警" and quiet_hit:
            return "重要告警突破静默投递"
        if priority == "重要告警":
            return "重要告警合并投递"
        return "合并投递"

    def _release_held(self, moment: datetime, recipient: str | None) -> int:
        """静默结束释放：同一接收人同一渠道的暂存批次合并成一次投递。"""
        held = [
            row for row in store.rows(MODULE)
            if row.get("status") == "静默暂存"
            and (not recipient or row.get("接收人") == recipient)
            and (end := parse_datetime(row.get("静默结束时间"))) is not None
            and end <= moment
        ]
        if not held:
            return 0
        buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in held:
            bucket = (str(row.get("接收人") or ""), str(row.get("推送渠道") or ""))
            buckets.setdefault(bucket, []).append(row)

        released = 0
        for rows in buckets.values():
            rows.sort(key=lambda row: (
                PRIORITY_ORDER.get(str(row.get("优先级")), 99),
                str(row.get("原定推送时间") or ""),
                str(row.get("批次号") or ""),
            ))
            primary = rows[0]
            channel = str(primary.get("推送渠道") or "")
            if len(rows) == 1:
                self._advance(primary, "投递中", moment, 投递结果="投递中")
                self._finish_delivery(primary, moment, channel, "静默结束投递", 静默判定="静默结束后投递")
            else:
                merged_ids = [str(row.get("批次号")) for row in rows[1:]]
                merged_titles = "；".join(
                    f"{row.get('消息标题')}" for row in rows[1:]
                )
                self._advance(primary, "投递中", moment, 投递结果="投递中")
                self._finish_delivery(
                    primary,
                    moment,
                    channel,
                    "静默结束合并投递",
                    合并批次列表=merged_ids,
                    消息摘要=f"{primary.get('消息摘要')}；静默结束合并：{merged_titles}",
                    静默判定="静默结束后合并投递",
                )
                for row in rows[1:]:
                    self._advance(
                        row,
                        "已合并投递",
                        moment,
                        投递结果="已并入静默结束合并投递",
                        合并主批次=primary.get("批次号"),
                        实际投递时间=format_datetime(moment),
                    )
            released += 1
        return released

    def _finish_delivery(
        self, record: dict[str, Any], moment: datetime, channel: str, result: str, **fields: Any
    ) -> None:
        if channel not in SUPPORTED_CHANNELS:
            self._advance(
                record,
                "投递失败",
                moment,
                投递结果="投递失败",
                失败原因=f"推送渠道「{channel}」未接入",
                实际投递时间=format_datetime(moment),
                **fields,
            )
            return
        self._advance(
            record,
            "已投递",
            moment,
            投递结果=result,
            实际投递时间=format_datetime(moment),
            **fields,
        )

    def _advance(self, record: dict[str, Any], target: str, moment: datetime, **fields: Any) -> bool:
        if target not in STAGE_RANK:
            raise PushError(f"未知处理阶段「{target}」")
        current = str(record.get("status") or "待合并")
        current_rank = STAGE_RANK.get(current, -1)
        target_rank = STAGE_RANK[target]
        if target_rank < current_rank:
            raise PushError(f"批次 {record.get('批次号')} 不能从「{current}」回退到「{target}」")
        if target_rank == current_rank:
            # 同一阶段重复推进视为重复投递，直接忽略，不重复写轨迹。
            return False
        record.update(fields)
        record["status"] = target
        record["pending"] = target not in TERMINAL_STAGES
        record["abnormal"] = target == "投递失败"
        record.setdefault("处理轨迹", []).append({"阶段": target, "时间": format_datetime(moment)})
        return True


push_service = SubscriptionPushService()
