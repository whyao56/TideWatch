"""tidewatch 的统一数据模型。

这里定义的结构**刻意不含任何平台字段**。微博、B站、抖音、公开数据集之间的
差异，全部在 adapters 层被消化掉，引擎看到的永远只有下面这几个对象。

这是「换数据源不用改引擎」能成立的前提，也是这个项目能被别人复用的地基。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def ensure_aware(dt: datetime | None) -> datetime | None:
    """把 naive datetime 当作 UTC 处理。

    混用 naive 和 aware 的 datetime 做减法会直接抛异常，而数据源五花八门，
    在入口统一掉这个坑比在每个信号里各写一遍 if 要划算。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class Level(str, Enum):
    """风险分级，三档。

    刻意只分三档而不是给一个连续的分数，原因和 VirusDetector 一样：
    使用者要的不是一个「0.7321」的玄学数字，而是「这条该不该人工看一眼」。
    分数用于排序，档位用于决策。
    """

    CLEAN = "clean"
    WEAK = "weak"
    STRONG = "strong"

    @property
    def label(self) -> str:
        return {"clean": "正常", "weak": "弱嫌疑", "strong": "强嫌疑"}[self.value]

    @property
    def rank(self) -> int:
        return {"clean": 0, "weak": 1, "strong": 2}[self.value]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Level):
            return NotImplemented
        return self.rank < other.rank


class SignalKind(str, Enum):
    """信号所属层级。三层是递进关系：文本 -> 账号 -> 协同。

    递进的含义是「可靠性递增」：文本信号最便宜但最容易被规避，
    协同信号最贵但最难伪造。所以聚合时协同信号的权重必须更高。
    """

    TEXT = "text"
    ACCOUNT = "account"
    COORDINATION = "coordination"


class TargetKind(str, Enum):
    """被判定对象的种类。"""

    ITEM = "item"    # 单条发言（评论 / 弹幕 / 帖子）
    ACTOR = "actor"  # 单个账号
    GROUP = "group"  # 疑似协同团伙（一组账号）


@dataclass(slots=True)
class Actor:
    """一个发言账号。平台无关。

    所有字段都有默认值 —— 因为真实数据里这些信息经常残缺，
    强制要求填满会让适配器变得不可用。缺失值由信号层按「不表态」处理。
    """

    actor_id: str
    name: str = ""
    created_at: datetime | None = None      # 注册时间
    followers: int = 0
    following: int = 0
    post_count: int = 0
    has_avatar: bool = True
    is_default_name: bool = False           # 是否形如「用户1234567」的默认昵称
    rename_count: int | None = None         # 改名次数（部分平台不提供）
    verified: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def age_days(self, now: datetime | None = None) -> float | None:
        """账号年龄（天）。注册时间缺失时返回 None，让信号自己决定怎么办。"""
        created = ensure_aware(self.created_at)
        if created is None:
            return None
        now = ensure_aware(now) or datetime.now(timezone.utc)
        return (now - created).total_seconds() / 86400

    @property
    def follower_ratio(self) -> float:
        """关注数 / (粉丝数 + 1)。

        用 +1 而不是判零分支，是为了让「一个粉丝都没有」这种情况自然落到
        一个大数值上，而不是产生 inf 或需要特判。值越大越像「只关注别人、
        没人关注它」——这是买来的号最典型的形状。
        """
        return self.following / (self.followers + 1)


@dataclass(slots=True)
class Item:
    """一条发言。平台无关。

    `topic` 是把讨论归组的键（视频号 / 帖子 id / 话题名）。
    协同层的时间爆发、文本聚类都在同一个 topic 内进行比较 —— 拿不同话题的
    评论互相比相似度是纯粹的噪声。
    """

    item_id: str
    actor_id: str
    text: str
    created_at: datetime | None = None
    topic: str = ""
    parent_id: str | None = None                # 被回复对象的 id
    mentions: tuple[str, ...] = ()              # @ 到的账号
    repost_of: str | None = None                # 转发的原帖 id
    likes: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_repost(self) -> bool:
        return bool(self.repost_of)

    @property
    def preview(self) -> str:
        """给报告用的短摘要。"""
        text = self.text.strip().replace("\n", " ")
        return text[:40] + ("…" if len(text) > 40 else "")


@dataclass(slots=True)
class Dataset:
    """一次分析的输入：一批发言 + 涉及到的账号。

    引擎的所有信号都只吃这个对象。适配器的唯一职责就是把任何来源的数据
    变成 Dataset —— 想接自己的数据源，只需要再写一个适配器。
    """

    items: list[Item] = field(default_factory=list)
    actors: dict[str, Actor] = field(default_factory=dict)
    source: str = ""

    def __len__(self) -> int:
        return len(self.items)

    def actor(self, actor_id: str) -> Actor:
        """取账号；不存在时补一个占位对象。

        好处是信号层不必到处写 `if actor is None`，代价是缺少账号信息时
        账号层的信号会自然地「不表态」（因为默认值都是中性的）。
        """
        got = self.actors.get(actor_id)
        if got is None:
            got = Actor(actor_id=actor_id)
            self.actors[actor_id] = got
        return got

    def by_topic(self) -> dict[str, list[Item]]:
        """按话题分组，协同层的基本工作单元。"""
        out: dict[str, list[Item]] = {}
        for item in self.items:
            out.setdefault(item.topic or "__all__", []).append(item)
        return out

    def by_actor(self) -> dict[str, list[Item]]:
        """按账号分组。"""
        out: dict[str, list[Item]] = {}
        for item in self.items:
            out.setdefault(item.actor_id, []).append(item)
        return out

    @property
    def time_span(self) -> tuple[datetime | None, datetime | None]:
        times = [ensure_aware(i.created_at) for i in self.items if i.created_at]
        if not times:
            return None, None
        return min(times), max(times)


@dataclass(slots=True)
class SignalHit:
    """某个信号对某个对象的单次判定。

    注意 `score` 是**基础分**，不是最终分 —— 加权和联动加分由聚合器负责。
    信号只负责回答「我看到了什么、有多可疑」，不负责回答「总共多少分」。
    这条职责边界让加一个新信号永远不会影响已有信号的实现。
    """

    signal: str
    kind: SignalKind
    target: str
    target_kind: TargetKind
    level: Level
    score: float
    reason: str
    weight: float = 1.0   # 该信号在配置里的权重，聚合时使用
    evidence: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.level.label}] {self.signal} -> {self.reason}"


@dataclass(slots=True)
class Assessment:
    """对一个对象的最终判定，含可解释的全部依据。"""

    target: str
    target_kind: TargetKind
    score: float
    level: Level
    hits: list[SignalHit] = field(default_factory=list)
    bonus: float = 0.0   # 联动加分（多个层级同时命中时的额外加权）
    breakdown: list[tuple[str, float]] = field(default_factory=list)  # (信号名, 该信号贡献分)
    promoted: bool = False  # 是否因「命中强信号」而被保底提升档位
    label: str = ""      # 人类可读标识：账号名或发言摘要

    @property
    def kinds_hit(self) -> set[str]:
        return {h.kind.value for h in self.hits}

    @property
    def reasons(self) -> list[str]:
        return [h.reason for h in self.hits]

    @property
    def by_kind(self) -> dict[str, list[SignalHit]]:
        """按层级归组，报告里用来分节展示。"""
        out: dict[str, list[SignalHit]] = {}
        for hit in self.hits:
            out.setdefault(hit.kind.value, []).append(hit)
        return out

    @property
    def is_flagged(self) -> bool:
        return self.level is not Level.CLEAN
