"""通用小工具：时间解析和一些与文本内容无关的杂项。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

__all__ = ["parse_time", "safe_div", "clamp"]

# 时间戳按位数分流的边界。10 位秒级约到 2286 年，13 位毫秒级才是真正的毫秒。
_MS_THRESHOLD = 1e11


def parse_time(value: Any) -> datetime | None:
    """尽量宽松地把各种来源的时间解析成 aware datetime。

    真实数据里的时间至少有四种形态，任何一种没处理，协同层的时间爆发检测
    就会整段失效 —— 因为时间成了 None，所有条目都进不了时间窗，而信号会
    安静地「什么都没发现」，这种失败最难察觉。

      1. 已经是 datetime；2. 秒级时间戳；3. 毫秒级时间戳；4. 日期字符串。

    第 3 种最容易误判：13 位数字按秒解释会得到公元 50000 年，
    结果是所有条目聚成一个「爆发」，误报整片。
    """
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = float(value)
        if ts > _MS_THRESHOLD:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.isdigit():
            return parse_time(int(raw))
        cleaned = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(cleaned)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    """除零安全的除法。水军数据里大量分母为 0（零粉丝、零原创），
    到处写 if 会让信号代码变得不可读。"""
    if denominator == 0:
        return default
    return numerator / denominator


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """把强度裁剪到合法区间。

    信号产出的「强度」最终会乘进分数，越界值不会报错、只会让分数悄悄跑偏，
    所以在信号出口处统一收口，而不是指望每个信号自己算对。
    """
    return max(low, min(high, value))
