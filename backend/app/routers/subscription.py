"""订阅推送接口：维护订阅配置，并执行按频率合并的推送、查看推送批次时间线。

注意路由声明顺序：`/datasets`、`/push-records`、`/timeline`、`/export` 都是静态路径，
必须排在 `/{entry_id}` 之前，否则会被当成 entry_id 解析而返回 422。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services import insights
from app.services.schedule import ScheduleError, parse_datetime
from app.services.subscription import SubscriptionService
from app.services.subscription_push import PushError, push_service

router = APIRouter(prefix="/api/subscription", tags=["订阅推送"])

service = SubscriptionService()

LIST_FIELDS = ["订阅编号", "订阅内容", "接收人", "推送渠道", "推送频率", "数据粒度", "静默时段", "下次推送时间"]
STATUSES = ["草稿", "订阅评审中", "已启用", "已暂停"]
PUSH_STATUSES = ["待合并", "已合并", "静默暂存", "投递中", "已投递", "已合并投递", "投递失败"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按订阅编号检索"),
    status: str | None = Query(default=None, description="草稿、订阅评审中、已启用、已暂停"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按订阅编号与状态过滤订阅推送列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/datasets")
def list_datasets() -> dict[str, Any]:
    """待推送分析数据集：日报、告警摘要、洞察快报的当前快照摘要。"""
    bundle = insights.build_push_datasets()
    return {
        "snapshot_version": bundle["snapshot_version"],
        "snapshot_generated_at": bundle["snapshot_generated_at"],
        "datasets": bundle["datasets"],
    }


@router.get("/push-records")
def list_push_records(
    keyword: str | None = Query(default=None, description="按批次号或消息标题检索"),
    status: str | None = Query(default=None, description="待合并、已合并、静默暂存、投递中、已投递、已合并投递、投递失败"),
    recipient: str | None = Query(default=None, description="按接收人过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """订阅推送数据记录：一次合并批次一条，可追溯到消息内容与处理轨迹。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = push_service.list_records(
        keyword=keyword, status=status, recipient=recipient, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/timeline")
def push_timeline(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    """推送时间线视图：按批次给出投递结果，概览里单独列出失败项。"""
    return push_service.timeline(limit=limit)


@router.post("/push")
def run_push(payload: EntryPayload) -> ActionResult:
    """按频率合并并推送订阅消息，生成订阅推送数据记录并推进下次推送时间。

    at 用于模拟/补推某个时刻到期的排期；缺省取当前时间。
    """
    at = payload.values.get("at")
    if at not in (None, "") and parse_datetime(at) is None:
        return ActionResult(ok=False, message=f"推送时刻「{at}」格式不正确，应为 YYYY-MM-DD HH:MM")
    recipient = str(payload.values.get("recipient") or "").strip() or None
    try:
        summary = push_service.dispatch(at=at or None, recipient=recipient)
    except (PushError, ScheduleError) as error:
        return ActionResult(ok=False, message=str(error))
    message = (
        f"已生成 {summary['generated']} 个推送批次：投递 {summary['delivered']}，"
        f"静默暂存 {summary['held']}，失败 {summary['failed']}，"
        f"释放静默暂存 {summary['released']}"
    )
    if summary["skipped"]:
        message += f"；跳过 {len(summary['skipped'])} 条配置"
    return ActionResult(ok=True, message=message, entry=summary)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出订阅推送清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "subscription", "total": total, "items": items}


@router.get("/push-records/{batch_id}")
def get_push_record(batch_id: str) -> dict:
    """读取单个推送批次明细；批次号不存在时给出可读说明。"""
    record = push_service.get_record(batch_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"推送批次 {batch_id} 不存在")
    return record


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条订阅配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"订阅配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条订阅配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message="；".join(missing))
    return ActionResult(ok=True, message="订阅配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条订阅配置执行提交订阅评审、启用订阅、暂停订阅；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
