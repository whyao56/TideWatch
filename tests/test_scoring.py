"""评分聚合的测试。

评分器是整个项目里唯一「把多个证据合成一个结论」的地方，也是最容易偷偷
出错的地方 —— 它的输出永远是个数字，看起来总是合理的。所以这里逐项验证
公式里的每一个因子，并且专门锁住三个刻意的设计：

  - 跨层联动加分只在**跨层**时生效
  - 命中强信号却算出「正常」时必须保底提档，且留下旗标
  - 团伙分传播给成员时，成员只拿打折的分、且一律不算强嫌疑
"""

from __future__ import annotations

import pytest

from conftest import make_assessment, make_hit
from tidewatch.scoring import Scorer, propagate_group_members
from tidewatch.types import Level, SignalKind, TargetKind

# --------------------------------------------------------------------------
# 单项因子
# --------------------------------------------------------------------------


def test_single_weak_hit_matches_formula(config) -> None:
    """裸公式：强度 × 档位倍率 × 信号权重 × 层权重 × scale。"""
    scorer = Scorer(config)
    hit = make_hit(kind=SignalKind.TEXT, level=Level.WEAK, score=1.0, weight=1.0)
    got = scorer.assess([hit], targets=[(TargetKind.ITEM, "i1")])[0]
    # 1.0 * 1.0(weak) * 1.0(信号权重) * 0.9(text 层权重) * 25 = 22.5
    assert got.score == pytest.approx(22.5)
    assert got.level is Level.WEAK
    assert got.breakdown == [("text.keyword", 22.5)]


def test_strong_level_contributes_more_than_weak(config) -> None:
    """同一个信号判 STRONG 必须比判 WEAK 贡献更多分。

    否则「信号自己很确信」这件事传不到总分上，强证据会被算术平均掉。
    """
    scorer = Scorer(config)
    weak = make_hit(level=Level.WEAK, score=1.0)
    strong = make_hit(level=Level.STRONG, score=1.0)
    weak_score = scorer.assess([weak], targets=[(TargetKind.ITEM, "i1")])[0].score
    strong_score = scorer.assess([strong], targets=[(TargetKind.ITEM, "i1")])[0].score
    assert strong_score > weak_score
    assert strong_score == pytest.approx(weak_score * 1.5)


def test_signal_weight_scales_contribution(config) -> None:
    """信号权重必须是**乘**进去的，不是被忽略的。

    回归：曾经因为配置键名带点号而查不到权重，所有信号静默回落到 1.0，
    分档全线偏移 —— 这种错不会报异常，只会让结论悄悄变松。
    """
    scorer = Scorer(config)
    low = make_hit(score=1.0, weight=0.5)
    high = make_hit(score=1.0, weight=2.0)
    low_score = scorer.assess([low], targets=[(TargetKind.ITEM, "i1")])[0].score
    high_score = scorer.assess([high], targets=[(TargetKind.ITEM, "i1")])[0].score
    assert high_score == pytest.approx(low_score * 4)


def test_kind_weight_prefers_coordination(config) -> None:
    """协同层的分数必须高于文本层 —— 这是「越难伪造越值钱」的落地。"""
    scorer = Scorer(config)
    text = make_hit(kind=SignalKind.TEXT, score=1.0)
    coord = make_hit(kind=SignalKind.COORDINATION, score=1.0)
    text_score = scorer.assess([text], targets=[(TargetKind.ITEM, "i1")])[0].score
    coord_score = scorer.assess([coord], targets=[(TargetKind.GROUP, "g1")])[0].score
    assert coord_score > text_score


# --------------------------------------------------------------------------
# 档位切分与保底
# --------------------------------------------------------------------------


def test_level_thresholds(config) -> None:
    """三档按 levels 里的阈值切，边界值属于上一档。"""
    scorer = Scorer(config)
    assert scorer.weak_threshold == 20
    assert scorer.strong_threshold == 50
    # 20 * 25 = 500 —— 用一个必然越界的组合来确认上下界都被 cap 住
    assert scorer._level_for(19.99) is Level.CLEAN
    assert scorer._level_for(20.0) is Level.WEAK
    assert scorer._level_for(49.99) is Level.WEAK
    assert scorer._level_for(50.0) is Level.STRONG


def test_strong_hit_is_promoted_when_arithmetic_says_clean(config) -> None:
    """回归：命中强信号却算出「正常」时，提档为弱嫌疑并留下旗标。

    分数很低是可能的（信号权重低、层权重低），但「我看到了强证据」这件事
    不该被算术平均掉。旗标 `promoted` 保证报告里能解释分数与档位为什么对不上。
    """
    scorer = Scorer(config)
    hit = make_hit(kind=SignalKind.ACCOUNT, level=Level.STRONG, score=0.1, weight=1.0)
    got = scorer.assess([hit], targets=[(TargetKind.ACTOR, "a1")])[0]
    assert got.score < scorer.weak_threshold  # 分数本身确实没到线
    assert got.level is Level.WEAK
    assert got.promoted is True


def test_clean_hit_keeps_clean_level(config) -> None:
    """没有强信号的正常条目不该被提档，旗标也不该亮。"""
    scorer = Scorer(config)
    hit = make_hit(kind=SignalKind.ACCOUNT, level=Level.WEAK, score=0.05)
    got = scorer.assess([hit], targets=[(TargetKind.ACTOR, "a1")])[0]
    assert got.level is Level.CLEAN
    assert got.promoted is False


def test_score_is_capped(config) -> None:
    """分数封顶，避免「命中信号多」被误读成「极其可疑」。"""
    scorer = Scorer(config)
    hits = [
        make_hit(signal=f"coordination.s{i}", kind=SignalKind.COORDINATION,
                 target="g1", target_kind=TargetKind.GROUP,
                 level=Level.STRONG, score=1.0, weight=2.0)
        for i in range(20)
    ]
    got = scorer.assess(hits, targets=[(TargetKind.GROUP, "g1")])[0]
    assert got.score == pytest.approx(scorer.cap)


# --------------------------------------------------------------------------
# 跨层联动
# --------------------------------------------------------------------------


def test_synergy_bonus_requires_cross_kind(config) -> None:
    """回归：同层命中多个信号**不**加分。

    同一层里的十个信号通常只是同一个原因的十种说法，不构成独立证据；
    只有跨层命中才意味着「不同类型的数据指向同一个结论」。
    """
    scorer = Scorer(config)
    same_kind = [
        make_hit(signal=f"text.s{i}", kind=SignalKind.TEXT, level=Level.WEAK, score=1.0)
        for i in range(3)
    ]
    got = scorer.assess(same_kind, targets=[(TargetKind.ITEM, "i1")])[0]
    assert got.bonus == 0.0

    cross_kind = [
        make_hit(signal="text.keyword", kind=SignalKind.TEXT, score=1.0),
        make_hit(signal="account.age", kind=SignalKind.ACCOUNT, score=1.0),
    ]
    got = scorer.assess(cross_kind, targets=[(TargetKind.ITEM, "i1")])[0]
    assert got.bonus == pytest.approx(scorer.bonus_per_extra)
    assert len(got.kinds_hit) == 2


def test_synergy_bonus_grows_with_kind_count(config) -> None:
    """每多命中一层，加成再多一档。"""
    scorer = Scorer(config)
    three = [
        make_hit(signal="text.keyword", kind=SignalKind.TEXT, score=0.1),
        make_hit(signal="account.age", kind=SignalKind.ACCOUNT, score=0.1),
        make_hit(signal="coordination.burst", kind=SignalKind.COORDINATION, score=0.1),
    ]
    got = scorer.assess(three, targets=[(TargetKind.ITEM, "i1")])[0]
    assert got.bonus == pytest.approx(scorer.bonus_per_extra * 2)


# --------------------------------------------------------------------------
# target 清单：区分「判为正常」与「没评估过」
# --------------------------------------------------------------------------


def test_targets_keeps_unhit_entries_visible(config) -> None:
    """回归：没命中任何信号的目标也要出现在结果里。

    不传 targets 的话它们会整个消失，评测时就没法区分「判定为正常」和
    「压根没跑到」——这两件事必须能分开，否则算不出召回率。
    """
    scorer = Scorer(config)
    hit = make_hit(target="i1")
    got = scorer.assess([hit], targets=[(TargetKind.ITEM, "i1"), (TargetKind.ITEM, "i2")])
    assert {a.target for a in got} == {"i1", "i2"}
    clean = next(a for a in got if a.target == "i2")
    assert clean.level is Level.CLEAN
    assert clean.hits == []


def test_without_targets_unhit_entries_disappear(config) -> None:
    """反过来确认默认行为：不传 targets 就只有命中过的目标。"""
    scorer = Scorer(config)
    got = scorer.assess([make_hit(target="i1")])
    assert {a.target for a in got} == {"i1"}


def test_results_sorted_by_score_desc(config) -> None:
    scorer = Scorer(config)
    hits = [
        make_hit(signal="text.a", target="i1", score=1.0),
        make_hit(signal="text.b", target="i2", score=3.0),
    ]
    got = scorer.assess(hits)
    assert [a.target for a in got] == ["i2", "i1"]


def test_labels_are_attached(config) -> None:
    scorer = Scorer(config)
    got = scorer.assess([make_hit(target="i1")], labels={"i1": "这是一条评论摘要"})
    assert got[0].label == "这是一条评论摘要"


# --------------------------------------------------------------------------
# 团伙 -> 成员传播
# --------------------------------------------------------------------------


def test_propagation_carries_group_score_to_members(config) -> None:
    """协同层产出的是团伙，但使用者要的是「到底哪些账号」。"""
    group_hit = make_hit(
        signal="coordination.burst",
        kind=SignalKind.COORDINATION,
        target="burst:t1",
        target_kind=TargetKind.GROUP,
        level=Level.STRONG,
        score=0.8,
        weight=1.4,
        evidence={"actors": ["a1", "a2", "a3"], "topic": "t1"},
    )
    group = make_assessment(target="burst:t1", score=80.0, hits=[group_hit])

    extra = propagate_group_members([group], factor=0.5)
    assert set(extra) == {"a1", "a2", "a3"}
    for member_hits in extra.values():
        assert len(member_hits) == 1
        carried = member_hits[0]
        assert carried.signal == "coordination.burst#member"
        assert carried.target_kind is TargetKind.ACTOR
        # 打折传入，且一律标 WEAK —— 团伙一旦误报会连坐全体成员，不能直接定案
        assert carried.score == pytest.approx(0.4)
        assert carried.level is Level.WEAK
        assert carried.weight == pytest.approx(1.4)
        assert carried.evidence["group"] == "burst:t1"


def test_propagation_ignores_clean_groups(config) -> None:
    """只从**已判可疑**的团伙往外传。判为正常的团伙不该连坐它的成员。"""
    group_hit = make_hit(
        target="cooccur:x",
        target_kind=TargetKind.GROUP,
        kind=SignalKind.COORDINATION,
        score=0.2,
        evidence={"members": ["a1", "a2"]},
    )
    clean_group = make_assessment(
        target="cooccur:x", score=5.0, level=Level.CLEAN, hits=[group_hit]
    )
    assert propagate_group_members([clean_group], factor=0.6) == {}


def test_propagation_skips_hits_without_members(config) -> None:
    """证据里没有成员名单时安静跳过，而不是抛异常。"""
    group_hit = make_hit(
        target="cluster:t1",
        target_kind=TargetKind.GROUP,
        kind=SignalKind.COORDINATION,
        score=0.9,
        evidence={"size": 3},
    )
    group = make_assessment(target="cluster:t1", score=70.0, hits=[group_hit])
    assert propagate_group_members([group], factor=0.6) == {}


def test_propagation_reads_both_evidence_keys(config) -> None:
    """burst 用 actors、cooccur 用 members —— 两个键都要认。

    这是跨信号的隐式契约：改信号里的证据字段名而不同步这里，
    传播会静默失效（协同层重新变成「只报团伙不报成员」）。
    """
    hit = make_hit(
        target="cooccur:x",
        target_kind=TargetKind.GROUP,
        kind=SignalKind.COORDINATION,
        score=0.9,
        evidence={"members": ["m1", "m2"]},
    )
    extra = propagate_group_members(
        [make_assessment(target="cooccur:x", score=70.0, hits=[hit])], factor=0.6
    )
    assert set(extra) == {"m1", "m2"}
