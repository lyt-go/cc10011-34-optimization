"""订阅排期计算：频率周期、频率窗口、静默时段判定与下次推送推进。

这些函数是纯函数，不读数据仓库，方便单独验证：
- 同一频率的订阅按窗口合并，窗口口径随频率变化；
- 静默判定按“时刻 + 静默时段”计算，跨零点也成立；
- 下次推送时间始终沿原排期锚点前进，不随实际投递时间漂移。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

FREQUENCY_PERIODS: dict[str, timedelta] = {
    "每日": timedelta(days=1),
    "每小时": timedelta(hours=1),
    "每15分钟": timedelta(minutes=15),
}

# 时段边界用固定宽度，便于窗口键稳定：每日按“日期+时分”，小时按整点，
# 15 分钟按 0/15/30/45 对齐。
FREQUENCY_STEP_MINUTES = {"每日": 1440, "每小时": 60, "每15分钟": 15}


class ScheduleError(ValueError):
    """排期参数不合法时抛出，由路由层转成可读提示。"""


def parse_datetime(value: object) -> datetime | None:
    """解析 ISO 风格时间；空值或无法识别时返回 None。"""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def format_datetime(value: datetime | None) -> str:
    """统一的时间展示口径，精确到分钟。"""
    return value.strftime("%Y-%m-%d %H:%M") if value else ""


def parse_frequency(value: object) -> timedelta:
    frequency = str(value or "").strip()
    if frequency not in FREQUENCY_PERIODS:
        raise ScheduleError(f"推送频率「{frequency}」无法识别，仅支持：{'、'.join(FREQUENCY_PERIODS)}")
    return FREQUENCY_PERIODS[frequency]


def normalize_frequency(value: object) -> str:
    frequency = str(value or "").strip()
    if frequency not in FREQUENCY_PERIODS:
        raise ScheduleError(f"推送频率「{frequency}」无法识别，仅支持：{'、'.join(FREQUENCY_PERIODS)}")
    return frequency


def frequency_window_key(moment: datetime, frequency: str) -> str:
    """把时刻落到频率窗口，作为合并批次的业务键。"""
    normalize_frequency(frequency)
    if frequency == "每日":
        # 每天同一“时刻”才算同一批次；09:00 与 23:30 是两班排期，不能混批。
        return moment.strftime("%Y-%m-%dT%H:%M")
    if frequency == "每小时":
        return moment.strftime("%Y-%m-%dT%H:00")
    minute = (moment.minute // 15) * 15
    return moment.replace(minute=minute, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")


def frequency_window_bounds(moment: datetime, frequency: str) -> tuple[datetime, datetime]:
    """返回时刻所在频率窗口的起止。"""
    normalize_frequency(frequency)
    if frequency == "每日":
        start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    if frequency == "每小时":
        start = moment.replace(minute=0, second=0, microsecond=0)
        return start, start + timedelta(hours=1)
    start = moment.replace(minute=(moment.minute // 15) * 15, second=0, microsecond=0)
    return start, start + timedelta(minutes=15)


def advance_to_next_anchor(anchor: datetime, frequency: str, after: datetime) -> datetime:
    """从 anchor 起按频率周期前进，返回严格晚于 after 的第一个锚点。

    以原锚点为基准而不是以投递时刻为基准，保证排期不漂移；
    例如每日 09:00 的订阅在 09:07 投递后，下次仍是次日 09:00。
    """
    period = parse_frequency(frequency)
    if period.total_seconds() <= 0:
        raise ScheduleError("频率周期必须大于 0")
    if after < anchor:
        return anchor
    steps = int((after - anchor) // period)
    candidate = anchor + period * steps
    while candidate <= after:
        candidate += period
    return candidate


def postpone_after_pause(scheduled: datetime, frequency: str, paused_at: datetime) -> datetime:
    """暂停时把下次推送整体顺延，避免恢复后立刻补发暂停期内容。

    基准取“原计划时间”和“实际暂停时间”的较晚者，再前进一个完整周期。
    """
    period = parse_frequency(frequency)
    base = max(scheduled, paused_at)
    return base + period


def parse_quiet_window(value: object) -> tuple[timedelta, timedelta] | None:
    """解析 "HH:MM-HH:MM" 静默时段；空值表示不设静默。"""
    text = str(value or "").strip()
    if not text:
        return None
    if "-" not in text:
        raise ScheduleError(f"静默时段「{text}」格式不正确，应为 HH:MM-HH:MM")
    start_text, end_text = (part.strip() for part in text.split("-", 1))
    start = _parse_clock(start_text)
    end = _parse_clock(end_text)
    if start == end:
        # 起止相同视为全天静默没有意义，按未配置处理，避免误拦全部消息。
        return None
    return start, end


def _parse_clock(text: str) -> timedelta:
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ScheduleError(f"静默时刻「{text}」格式不正确，应为 HH:MM")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"静默时刻「{text}」超出 00:00-23:59 范围")
    return timedelta(hours=hour, minutes=minute)


def in_quiet_hours(moment: datetime, quiet: object) -> bool:
    """判断时刻是否落在静默时段内，支持 22:00-08:00 这类跨零点区间。"""
    window = parse_quiet_window(quiet)
    if window is None:
        return False
    start, end = window
    clock = timedelta(hours=moment.hour, minutes=moment.minute, seconds=moment.second)
    if start < end:
        return start <= clock < end
    return clock >= start or clock < end


def quiet_end(moment: datetime, quiet: object) -> datetime | None:
    """返回该时刻所处静默时段的结束时间；不在静默内时返回 None。"""
    window = parse_quiet_window(quiet)
    if window is None or not in_quiet_hours(moment, quiet):
        return None
    start, end = window
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    end_at = midnight + end
    if start < end:
        return end_at
    # 跨零点：22:00-08:00 的前半夜结束在次日，凌晨时刻结束在当天。
    if timedelta(hours=moment.hour, minutes=moment.minute) >= start:
        return end_at + timedelta(days=1)
    return end_at


def quiet_windows_text(items: Iterable[object]) -> str:
    """把一组订阅的静默时段去重拼成展示文本。"""
    seen: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.append(text)
    return "、".join(seen) if seen else "未设置"
