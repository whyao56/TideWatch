"""三层信号的测试。

重点是几条**回归测试**，每一条都对应一个「跑得通但结论是错的」缺陷：
  - 协同层未校验跨账号 -> 单人刷屏被当成团伙
  - 账号年龄用 now() 算 -> 分析历史数据时结论完全反过来
  - 规避信号未做差集 -> 正常话术被误报成「刻意规避」
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import BASE_TIME, make_actor, make_item
from tidewatch.signals import build_signals
from tidewatch.types import Dataset, Level

# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def signal_map(config, rules):
    return {s.name: s for s in build_signals(config, rules)}


def hits_for(signal, dataset, target_kind=None):
    hits = signal.run(dataset)
    if target_kind is not None:
        hits = [h for h in hits if h.target_kind is target_kind]
    return hits


# --------------------------------------------------------------------------
# 文本层
# --------------------------------------------------------------------------


def test_evasion_only_reports_extra_matches(config, rules) -> None:
    """规避信号报的必须是「靠折叠才捞出来的」那部分。

    如果它把原文就能匹配到的也报出来，那它和 keyword 信号就没有区别了，
    「刻意规避」这个高精度判断会被稀释成噪声。
    """
    sig = signal_map(config, rules)["text.evasion"]
    normal = Dataset(items=[make_item("a", "u1", "有需要的加微信详聊")], actors={})
    evaded = Dataset(items=[make_item("b", "u2", "有需要的加薇❤️信详聊")], actors={})

    # 原文就命中「加微信」的话，不算规避
    assert hits_for(sig, normal) == []
    # 折叠后才命中的才算
    assert len(hits_for(sig, evaded)) == 1
    assert hits_for(sig, evaded)[0].level is Level.STRONG


def test_keyword_matches_phrases_and_regex(config, rules) -> None:
    sig = signal_map(config, rules)["text.keyword"]
    data = Dataset(
        items=[
            make_item("a", "u1", "详情私聊我，加微信谈价格"),
            make_item("b", "u2", "我的手机号是 13800138000"),
            make_item("c", "u3", "这个方案在真实场景下还需要更多验证"),
        ],
        actors={},
    )
    hits = hits_for(sig, data)
    targets = {h.target for h in hits}
    assert targets == {"a", "b"}
    assert all(h.level is Level.STRONG for h in hits)


def test_template_detects_internal_repetition(config, rules) -> None:
    sig = signal_map(config, rules)["text.template"]
    data = Dataset(
        items=[
            make_item("a", "u1", "这个方法好用这个方法好用这个方法好用这个方法好用"),
            make_item("b", "u2", "这个方案在真实场景里的表现还需要更多验证才能下结论"),
        ],
        actors={},
    )
    assert {h.target for h in hits_for(sig, data)} == {"a"}


def test_repetition_detects_flooding(config, rules) -> None:
    sig = signal_map(config, rules)["text.repetition"]
    data = Dataset(
        items=[
            make_item("a", "u1", "哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈"),
            make_item("b", "u2", "这个方案在真实场景里的表现还需要更多验证"),
        ],
        actors={},
    )
    assert {h.target for h in hits_for(sig, data)} == {"a"}


# --------------------------------------------------------------------------
# 账号层
# --------------------------------------------------------------------------


def test_account_age_uses_dataset_time_not_now(config, rules) -> None:
    """回归：账号年龄必须相对于**数据的时间范围**算，不能用 datetime.now()。

    用当前时刻算，分析一份去年的数据时，所有账号都会被算成「注册了一年」，
    年龄信号会集体失效 —— 而且失败得很安静，什么都不报。
    """
    sig = signal_map(config, rules)["account.age"]
    actor = make_actor("f1", created_at=BASE_TIME - timedelta(days=2))
    item = make_item("i1", "f1", "一条内容", created_at=BASE_TIME)
    data = Dataset(items=[item], actors={"f1": actor})

    hits = hits_for(sig, data)
    assert len(hits) == 1
    assert hits[0].evidence["age_days"] == pytest.approx(2.0, abs=0.1)


def test_account_age_abstains_without_timestamp(config, rules) -> None:
    sig = signal_map(config, rules)["account.age"]
    actor = make_actor("f1", created_at=None)
    data = Dataset(items=[make_item("i1", "f1", "内容")], actors={"f1": actor})
    assert hits_for(sig, data) == []


def test_originality_detects_self_repetition(config, rules) -> None:
    sig = signal_map(config, rules)["account.originality"]
    line = "这个教程真的写得太好了建议大家一定都要认真看完"
    items = [
        make_item(f"i{k}", "f1", line, created_at=BASE_TIME + timedelta(seconds=k * 60))
        for k in range(6)
    ]
    data = Dataset(items=items, actors={"f1": make_actor("f1")})
    hits = hits_for(sig, data)
    assert len(hits) == 1
    assert hits[0].target == "f1"


def test_originality_abstains_on_small_sample(config, rules) -> None:
    sig = signal_map(config, rules)["account.originality"]
    line = "这个教程真的写得太好了建议大家一定都要认真看完"
    items = [make_item(f"i{k}", "f1", line) for k in range(3)]
    data = Dataset(items=items, actors={"f1": make_actor("f1")})
    assert hits_for(sig, data) == []


def test_profile_requires_feature_stack(config, rules) -> None:
    """单个弱特征不该触发，多个叠加才算 —— 这是这层不大量误报的关键。"""
    sig = signal_map(config, rules)["account.profile"]

    only_avatar = Dataset(
        items=[], actors={"a": make_actor("a", has_avatar=False)}
    )
    assert hits_for(sig, only_avatar) == []

    stacked = Dataset(
        items=[],
        actors={
            "b": make_actor(
                "b", has_avatar=False, is_default_name=True, followers=0, following=800
            )
        },
    )
    hits = hits_for(sig, stacked)
    assert len(hits) == 1
    assert hits[0].level is Level.STRONG


# --------------------------------------------------------------------------
# 协同层
# --------------------------------------------------------------------------


def _coordination_dataset(
    actor_ids: list[str],
    texts: list[str],
    step: int,
    topic: str,
    *,
    background: int = 0,
    bg_step: int = 180,
) -> Dataset:
    """构造一份（可选带安静背景的）协同场景数据。

    `background` 是另一话题下平缓铺开的正常发言条数。它不是为了凑数：
    时间爆发是**相对**判断，没有背景速率就无从谈起，加进来是让夹具
    与真实数据同构 —— 真实的评论流里永远既有自然讨论也有一小撮集中投放。
    """
    actors = {a: make_actor(a) for a in actor_ids}
    items = [
        make_item(
            f"i{k}",
            actor_ids[k % len(actor_ids)],
            texts[k % len(texts)],
            created_at=BASE_TIME + timedelta(seconds=k * step),
            topic=topic,
        )
        for k in range(len(actor_ids))
    ]
    for k in range(background):
        bg_actor = f"bg{k}"
        actors[bg_actor] = make_actor(bg_actor)
        items.append(
            make_item(
                f"bg{k}",
                bg_actor,
                f"路过说两句，这是第{k}条普通发言",
                created_at=BASE_TIME + timedelta(seconds=k * bg_step),
                topic="background",
            )
        )
    return Dataset(items=items, actors=actors)


def test_cluster_requires_multiple_actors(config, rules) -> None:
    """回归：文本聚类必须来自多个**不同账号**。

    不校验的话，一个人复制粘贴发三遍同样的话，就会被报成「协同团伙」——
    那是账号层的原创度问题，不是协同。
    """
    sig = signal_map(config, rules)["coordination.cluster"]
    text = "这个方法真的太好用了强烈推荐大家试一下绝对不会后悔"

    single = _coordination_dataset(["f1"] * 3, [text], step=10, topic="t1")
    assert hits_for(sig, single) == []

    multi_actors = _coordination_dataset(["f1", "f2", "f3"], [text], step=10, topic="t1")
    hits = hits_for(sig, multi_actors)
    assert len(hits) == 1
    assert hits[0].evidence["size"] == 3


def test_burst_requires_multiple_actors(config, rules) -> None:
    """回归：时间爆发同样必须跨账号。一个人连发十八条是刷屏，不是协同。"""
    sig = signal_map(config, rules)["coordination.burst"]

    single = _coordination_dataset(
        ["f1"] * 8, ["支持一下"], step=2, topic="t1", background=40
    )
    assert hits_for(sig, single) == []

    multi_actors = _coordination_dataset(
        [f"f{i}" for i in range(8)], ["支持一下"], step=2, topic="t1", background=40
    )
    hits = hits_for(sig, multi_actors)
    assert len(hits) == 1
    assert hits[0].evidence["actor_count"] == 8


def test_burst_ignores_small_samples(config, rules) -> None:
    sig = signal_map(config, rules)["coordination.burst"]
    data = _coordination_dataset(
        [f"f{i}" for i in range(3)], ["支持"], step=2, topic="t1", background=40
    )
    assert hits_for(sig, data) == []


def test_burst_ignores_evenly_spread(config, rules) -> None:
    """均匀分布的正常讨论不该被报成爆发。"""
    sig = signal_map(config, rules)["coordination.burst"]
    data = _coordination_dataset(
        [f"f{i}" for i in range(10)], ["各说各的"], step=1200, topic="t1", background=40
    )
    assert hits_for(sig, data) == []


def test_burst_abstains_without_background(config, rules) -> None:
    """回归：整批数据就是一段紧挨着的集中投放时，信号必须**弃权**而不是硬报。

    爆发是相对量。如果数据里根本不存在平缓的正常活动，就没有参照物，
    此时的比值必然接近 1 —— 报出来只会是噪声。这条测试锁住「没有背景就
    不下结论」这个诚实行为，防止以后为了「提高召回」把阈值调成绝对值。
    """
    sig = signal_map(config, rules)["coordination.burst"]
    data = _coordination_dataset(
        [f"f{i}" for i in range(8)], ["支持一下"], step=2, topic="t1", background=0
    )
    assert hits_for(sig, data) == []


def test_cooccur_finds_overlapping_actors(config, rules) -> None:
    sig = signal_map(config, rules)["coordination.cooccur"]
    actors = [f"f{i}" for i in range(4)]
    items = []
    for topic_index in range(4):
        for k, actor in enumerate(actors):
            items.append(
                make_item(
                    f"i{topic_index}-{k}",
                    actor,
                    f"第{topic_index}个话题的第{k}条内容",
                    topic=f"topic-{topic_index}",
                )
            )
    data = Dataset(items=items, actors={a: make_actor(a) for a in actors})
    hits = hits_for(sig, data)
    assert len(hits) == 1
    assert hits[0].evidence["member_count"] == 4


def test_cooccur_rejects_single_topic_overlap(config, rules) -> None:
    """只在同一个话题里共同出现不算 —— 一个大视频底下随便两个人都会「共同出现」。"""
    sig = signal_map(config, rules)["coordination.cooccur"]
    actors = [f"f{i}" for i in range(4)]
    items = [make_item(f"i{k}", a, "内容", topic="same") for k, a in enumerate(actors)]
    data = Dataset(items=items, actors={a: make_actor(a) for a in actors})
    assert hits_for(sig, data) == []


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------


def test_all_signals_exposed_via_config(config, rules) -> None:
    """配置里声明的信号必须都能被实例化，反之亦然。

    这条测试防的是「配置改了名字但代码没跟上」这类静默失配 ——
    权重悄悄回落默认值这种坑踩过一次就够了。
    """
    configured = {name for name, _ in config.iter_signals()}
    registered = set(signal_map(config, rules))
    assert configured == registered
