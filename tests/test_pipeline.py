"""端到端流水线的测试。

流水线本身很薄（只做编排和计时），所以这里验证的不是「算法对不对」，
而是几件编排层面容易出错的事：

  - 未命中任何信号的目标也必须出现在结果里（否则算不出召回率）
  - 团伙分要能传播到成员账号，且传播后要重新评分
  - 关掉某一层要能真的关掉（消融实验的正确性依赖这一点）
"""

from __future__ import annotations

import pytest

from tidewatch.pipeline import Pipeline
from tidewatch.signals import build_signals
from tidewatch.types import Level, SignalKind, TargetKind


@pytest.fixture
def pipeline(config, rules) -> Pipeline:
    return Pipeline(config, rules=rules)


# --------------------------------------------------------------------------
# 基本编排
# --------------------------------------------------------------------------


def test_run_returns_assessments_for_every_item_and_actor(pipeline, dataset) -> None:
    """回归：所有 item 和 actor 都必须被评估到。

    只返回「命中过信号的目标」时，评测脚本无法区分「判定为正常」和
    「压根没跑到」，召回率就没法算 —— 而召回率恰恰是水军识别最该看的指标。
    """
    result = pipeline.run(dataset)
    assessed_items = {a.target for a in result.assessments if a.target_kind is TargetKind.ITEM}
    assessed_actors = {a.target for a in result.assessments if a.target_kind is TargetKind.ACTOR}
    assert assessed_items == {i.item_id for i in dataset.items}
    assert set(dataset.actors) <= assessed_actors


def test_run_collects_timings_for_every_signal(pipeline, dataset) -> None:
    result = pipeline.run(dataset)
    for signal in pipeline.signals:
        assert signal.name in result.timings
        assert result.timings[signal.name] >= 0
    assert "scoring" in result.timings


def test_meta_describes_the_run(pipeline, dataset) -> None:
    meta = pipeline.run(dataset).meta
    assert meta["items"] == len(dataset.items)
    assert meta["actors"] == len(dataset.actors)
    assert meta["topics"] == len(dataset.by_topic())
    assert meta["source"] == "test"
    assert len(meta["config_fingerprint"]) == 12
    assert set(meta["signals"]) == {s.name for s in pipeline.signals}


def test_hits_all_point_at_known_targets(pipeline, dataset) -> None:
    """每条命中都必须指向一个真实存在的目标，否则报告里会出现幽灵条目。"""
    result = pipeline.run(dataset)
    known = {i.item_id for i in dataset.items} | set(dataset.actors)
    for hit in result.hits:
        if hit.target_kind is TargetKind.GROUP:
            continue
        assert hit.target in known, f"{hit.signal} 指向了不存在的目标 {hit.target}"


# --------------------------------------------------------------------------
# 三层都要真的被触发（夹具就是为此设计的）
# --------------------------------------------------------------------------


def test_fixture_triggers_all_three_layers(pipeline, dataset) -> None:
    """夹具必须让三层都有产出，否则其它测试可能在「什么都没发生」的情况下通过。"""
    kinds = {hit.kind for hit in pipeline.run(dataset).hits}
    assert kinds == {SignalKind.TEXT, SignalKind.ACCOUNT, SignalKind.COORDINATION}


def test_template_cluster_is_found(pipeline, dataset) -> None:
    result = pipeline.run(dataset)
    cluster = [h for h in result.hits if h.signal == "coordination.cluster"]
    assert len(cluster) == 1
    assert set(cluster[0].evidence["actors"]) == {"f1", "f2", "f3"}


def test_time_burst_is_found_with_background(pipeline, dataset) -> None:
    """时间爆发只有在存在背景速率时才该被检出，夹具里正好有。"""
    result = pipeline.run(dataset)
    burst = [h for h in result.hits if h.signal == "coordination.burst"]
    assert len(burst) == 1
    assert burst[0].evidence["actor_count"] == 8
    assert burst[0].evidence["density_ratio"] > 2.5


def test_normal_topic_is_not_flagged(pipeline, dataset) -> None:
    """回归：铺得很开的正常讨论一条都不该被抓。

    这是整个项目最容易翻车的地方 —— 规则一放松，正常用户先被误伤。
    """
    result = pipeline.run(dataset)
    normal_ids = {i.item_id for i in dataset.items if i.topic == "t-normal"}
    flagged = {
        a.target
        for a in result.assessments
        if a.target_kind is TargetKind.ITEM and a.is_flagged
    }
    assert not (normal_ids & flagged)


# --------------------------------------------------------------------------
# 团伙 -> 成员传播
# --------------------------------------------------------------------------


def test_group_members_are_promoted_into_results(pipeline, dataset) -> None:
    """回归：协同层只产出团伙，但使用者要的是「具体哪些账号」。

    不传播的话，一个完全由水军组成的团伙被挖出来了，成员账号在报告里
    却一行都看不到 —— 结论是对的，但不可用。
    """
    result = pipeline.run(dataset)
    member_hits = [h for h in result.hits if h.signal.endswith("#member")]
    assert member_hits, "协同层没有把团伙成员传播出来"

    propagated = {h.target for h in member_hits}
    assert "f4" in propagated  # 爆发团伙成员
    assert "f1" in propagated  # 模板簇成员


def test_propagated_evidence_is_always_weak(pipeline, dataset) -> None:
    """传播过来的证据一律标 WEAK —— 团伙一旦误报会连坐全体成员，不能直接定案。"""
    result = pipeline.run(dataset)
    propagated = [h for h in result.hits if h.signal.endswith("#member")]
    assert propagated, "没有传播出任何成员证据，这条测试没有验证到东西"
    assert all(h.level is Level.WEAK for h in propagated)
    assert all(h.weight > 0 for h in propagated)


def test_propagated_score_is_discounted(pipeline, dataset) -> None:
    """成员拿到的分必须是团伙分的**打折**版本，而且能追溯到来源团伙。"""
    result = pipeline.run(dataset)
    propagated = [h for h in result.hits if h.signal.endswith("#member")]
    for hit in propagated:
        assert 0 < hit.score < 1.0
        assert hit.evidence["group"].startswith(("burst:", "cluster:", "cooccur:"))
        assert hit.evidence["group_score"] > 0


def test_propagation_can_be_disabled(config, rules, dataset) -> None:
    """关掉传播后就不该再有 #member 命中 —— 保证开关是真的有效。"""
    off = config.with_overrides("scoring.propagation.enabled=false")
    result = Pipeline(off, rules=rules).run(dataset)
    assert not [h for h in result.hits if h.signal.endswith("#member")]


# --------------------------------------------------------------------------
# 消融：关掉一层要真的关掉
# --------------------------------------------------------------------------


def test_disabling_a_kind_removes_its_hits(config, rules, dataset) -> None:
    """消融实验的正确性完全依赖这个行为：关掉某层后该层不该再产出任何命中。"""
    all_signals = build_signals(config, rules)
    for kind in ("text", "account", "coordination"):
        disabled = [
            f"signals.{s.name}.enabled=false" for s in all_signals if s.name.startswith(f"{kind}.")
        ]
        pipeline = Pipeline(config.with_overrides(*disabled), rules=rules)
        result = pipeline.run(dataset)
        assert not [h for h in result.hits if h.kind.value == kind], f"{kind} 层没被关掉"
        # 其余层必须还在跑，否则「关掉一层」变成了「什么都没跑」
        assert len(pipeline.signals) == len(all_signals) - len(disabled)
        assert pipeline.signals


def test_with_overrides_does_not_mutate_original(config, rules) -> None:
    """回归：派生变体绝不能污染基线配置。

    消融实验里七个变体共用一份基线，任何一个漏改回来都会让后面的组
    带着错误的参数跑，而结果看起来完全正常 —— 这类错最难发现。
    """
    baseline = len(build_signals(config, rules))
    variant = config.with_overrides("signals.text.keyword.enabled=false")
    assert len(build_signals(variant, rules)) == baseline - 1
    assert len(build_signals(config, rules)) == baseline
    assert config.fingerprint() != variant.fingerprint()


def test_flagged_filters_by_level(pipeline, dataset) -> None:
    result = pipeline.run(dataset)
    assert all(a.level is not Level.CLEAN for a in result.flagged())
    strong = result.flagged(Level.STRONG)
    assert all(a.level is Level.STRONG for a in strong)
    assert len(strong) <= len(result.flagged())


def test_summary_is_consistent_with_assessments(pipeline, dataset) -> None:
    result = pipeline.run(dataset)
    stats = result.summary
    assert stats["assessed"] == len(result.assessments)
    assert stats["clean"] + stats["weak"] + stats["strong"] == stats["assessed"]
    assert stats["flagged"] == stats["weak"] + stats["strong"]
