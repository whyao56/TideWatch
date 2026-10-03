"""pytest 公共配置：把 src 加入 sys.path，并固定随机种子。"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tidewatch.config import Config  # noqa: E402
from tidewatch.rules import RuleBook  # noqa: E402
from tidewatch.types import (  # noqa: E402
    Actor,
    Assessment,
    Dataset,
    Item,
    Level,
    SignalHit,
    SignalKind,
    TargetKind,
)

BASE_TIME = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def rules() -> RuleBook:
    return RuleBook.load(ROOT / "rules")


@pytest.fixture
def config() -> Config:
    return Config.load()


@pytest.fixture
def rng() -> random.Random:
    return random.Random(20261003)


def make_actor(actor_id: str, name: str = "", **kwargs) -> Actor:
    defaults = {
        "created_at": BASE_TIME - timedelta(days=400),
        "followers": 120,
        "following": 80,
        "has_avatar": True,
        "is_default_name": False,
    }
    defaults.update(kwargs)
    return Actor(actor_id=actor_id, name=name or actor_id, **defaults)


def make_item(item_id: str, actor_id: str, text: str, **kwargs) -> Item:
    defaults = {"created_at": BASE_TIME, "topic": "t1"}
    defaults.update(kwargs)
    return Item(item_id=item_id, actor_id=actor_id, text=text, **defaults)


def make_hit(
    signal: str = "text.keyword",
    kind: SignalKind = SignalKind.TEXT,
    target: str = "i1",
    target_kind: TargetKind = TargetKind.ITEM,
    level: Level = Level.WEAK,
    score: float = 1.0,
    weight: float = 1.0,
    reason: str = "测试用命中",
    evidence: dict | None = None,
) -> SignalHit:
    """造一条信号命中，用于单独测试评分器而不必跑真信号。"""
    return SignalHit(
        signal=signal,
        kind=kind,
        target=target,
        target_kind=target_kind,
        level=level,
        score=score,
        weight=weight,
        reason=reason,
        evidence=evidence if evidence is not None else {},
    )


def make_assessment(
    target: str = "g1",
    target_kind: TargetKind = TargetKind.GROUP,
    score: float = 80.0,
    level: Level = Level.STRONG,
    hits: list[SignalHit] | None = None,
) -> Assessment:
    return Assessment(
        target=target,
        target_kind=target_kind,
        score=score,
        level=level,
        hits=hits if hits is not None else [],
    )


@pytest.fixture
def dataset() -> Dataset:
    """一份「三层都能真实触发」的混合数据集。

    这里刻意包含一段**平缓的正常活动**（长时间跨度 + 慢速到达），而不是只有
    几种异常挤在一起。原因是协同层的时间爆发是**相对**判断：没有背景速率，
    它就该弃权。夹具里如果没有背景，测试验证的其实是「弃权」而不是「检出」，
    那是一种看起来通过、实际什么都没测到的假测试。
    """
    actors: dict[str, Actor] = {}
    for i in range(1, 7):
        actors[f"u{i}"] = make_actor(f"u{i}")
    # 水军账号：新号、无头像、默认昵称、只关注别人 —— 账号层会看到它们
    for i in range(1, 12):
        actors[f"f{i}"] = make_actor(
            f"f{i}", has_avatar=False, is_default_name=True, followers=0, following=500
        )

    items: list[Item] = []

    # 背景：12 条正常发言铺在 2.75 小时里，每 15 分钟一条。
    #
    # 这 12 句必须**真的各不相同**，不能是「同一个模板换几个词」：聚类信号
    # 看的是最长公共**子串**，如果它们共享一段十几字的公共尾部，它们自己
    # 就会被当成一个模板簇 —— 夹具会先把自己坑掉，测的就不是正常的讨论了。
    normal_texts = [
        "看了三遍才把这段并发模型理顺，之前一直以为是锁的问题",
        "文档里那个示例跑不起来，我改成异步写法之后才正常",
        "up主能不能讲讲超时重试这块，生产环境踩过坑",
        "我们线上用的是另一种方案，简单但吞吐低一些",
        "有个疑问，缓存失效的瞬间会不会直接打到数据库",
        "补充一点经验：压测的时候记得先把日志关掉",
        "这个思路挺巧的，不过长期维护成本可能不低",
        "刚在测试环境复现了，确实是边界条件没处理",
        "视频节奏有点快，第二部分的公式我没跟上",
        "建议加一节讲监控指标，真正落地时很关键",
        "我们团队之前也讨论过，最后选了更保守的做法",
        "感谢分享，正好下周要重构这块，先收藏了",
    ]
    for k in range(12):
        items.append(
            make_item(
                f"n{k}",
                f"u{k % 6 + 1}",
                normal_texts[k],
                created_at=BASE_TIME + timedelta(seconds=900 * k),
                topic="t-normal",
            )
        )

    # 模板簇：3 个不同账号发几乎相同的话（协同层·文本聚类）
    for i, suffix in enumerate(["", "的", "。"]):
        items.append(
            make_item(
                f"tpl{i}",
                f"f{i + 1}",
                f"这个方法真的太好用了强烈推荐大家试一下绝对不会后悔{suffix}",
                created_at=BASE_TIME + timedelta(seconds=60 * i),
                topic="t-template",
            )
        )

    # 爆发窗口：8 个账号在 35 秒内集中发言（协同层·时间爆发）
    # 文案刻意各不相同，让这一组只被时间维度抓到，和模板簇分开
    burst_texts = [
        "刚看完，确实讲得不错",
        "前来支持一下",
        "这个观点我认同",
        "已经三连了，加油",
        "希望多出这类内容",
        "讲得很清楚，学到了",
        "蹲一个后续更新",
        "转给朋友看了",
    ]
    for i, text in enumerate(burst_texts):
        items.append(
            make_item(
                f"burst{i}",
                f"f{i + 4}",
                text,
                created_at=BASE_TIME + timedelta(seconds=5 * i),
                topic="t-burst",
            )
        )

    return Dataset(items=items, actors=actors, source="test")
