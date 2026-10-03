"""报告渲染的测试。

报告不是附属品，它是迭代闭环的入口：使用者看完能指出「这条我认、那条
我不认」，这些反馈才会变成下一轮的规则改动。所以这里除了验证渲染不崩，
更要验证几件**必须被显式表达出来**的信息不会被悄悄省掉：

  - 区分「判为正常」和「现有规则没覆盖」
  - 保底提升过档位时，要把这件事写出来，否则分数与档位对不上
  - 跨层加分要说明来自哪几层
"""

from __future__ import annotations

import json

from conftest import make_assessment, make_hit
from tidewatch.report import (
    render_json,
    render_markdown,
    render_text,
    summarize,
    to_dict,
)
from tidewatch.types import Level, SignalKind, TargetKind


def _sample():
    item = make_assessment(
        target="i1",
        target_kind=TargetKind.ITEM,
        score=88.5,
        level=Level.STRONG,
        hits=[
            make_hit(signal="text.keyword", kind=SignalKind.TEXT, target="i1",
                     level=Level.STRONG, score=0.9, reason="命中导流话术"),
            make_hit(signal="account.age", kind=SignalKind.ACCOUNT, target="i1",
                     level=Level.WEAK, score=0.5, reason="账号注册仅 3 天"),
        ],
    )
    item.bonus = 8.0
    clean_but_uncovered = make_assessment(
        target="i2", target_kind=TargetKind.ITEM, score=0.0, level=Level.CLEAN
    )
    promoted = make_assessment(
        target="a1",
        target_kind=TargetKind.ACTOR,
        score=10.5,
        level=Level.WEAK,
        hits=[make_hit(signal="account.profile", kind=SignalKind.ACCOUNT,
                       target="a1", target_kind=TargetKind.ACTOR,
                       level=Level.STRONG, score=0.4)],
    )
    promoted.promoted = True
    return [item, clean_but_uncovered, promoted]


# --------------------------------------------------------------------------
# 概览
# --------------------------------------------------------------------------


def test_summarize_separates_no_signal_from_clean() -> None:
    """回归：`no_signal` 和 `clean` 必须分开统计。

    「触发了信号但分数没到线」和「压根没触发任何信号」是两件事：
    前者说明规则太严，后者说明规则没覆盖到。混在一起就无从判断该往哪调。
    """
    stats = summarize(_sample())
    assert stats["assessed"] == 3
    assert stats["no_signal"] == 1          # i2 完全没命中
    assert stats["clean"] == 1              # 它是「正常」，但不是「没覆盖」
    assert stats["weak"] == 1
    assert stats["strong"] == 1
    assert stats["flagged"] == 2


def test_summarize_counts_flagged_by_kind() -> None:
    stats = summarize(_sample())
    assert stats["flagged_by_kind"] == {"item": 1, "actor": 1, "group": 0}


def test_summarize_lists_top_signals() -> None:
    stats = summarize(_sample())
    assert ("text.keyword", 1) in stats["top_signals"]


def test_summarize_on_empty_input() -> None:
    stats = summarize([])
    assert stats["assessed"] == 0
    assert stats["top_signals"] == []


# --------------------------------------------------------------------------
# 人读
# --------------------------------------------------------------------------


def test_render_text_shows_level_label_and_score() -> None:
    out = render_text(_sample(), limit=None)
    assert "【强嫌疑】" in out
    assert "88.5 分" in out
    assert "命中导流话术" in out


def test_render_text_marks_promoted_assessment() -> None:
    """回归：保底提升过档位时必须在报告里写出来。

    否则使用者会看到「10.5 分 · 弱嫌疑」这种分数与档位明显对不上的组合，
    只会怀疑是 bug，而不知道背后有一条强信号在兜底。
    """
    out = render_text(_sample(), limit=None)
    assert "保底提升" in out


def test_render_text_explains_cross_kind_bonus() -> None:
    out = render_text(_sample(), limit=None)
    assert "跨层联动加分" in out
    assert "2 个层级" in out


def test_render_text_groups_hits_by_layer_order() -> None:
    """层内按 文本 -> 账号 -> 协同 排序，读起来才是「由浅入深」。"""
    out = render_text(_sample(), limit=None)
    assert out.index("[文本]") < out.index("[账号]")


def test_render_text_filters_by_min_level() -> None:
    out = render_text(_sample(), limit=None, min_level=Level.STRONG)
    assert "i2" not in out
    assert "a1" not in out


def test_render_text_respects_limit() -> None:
    out = render_text(_sample(), limit=1)
    assert out.count("──") == 1


def test_render_text_empty_message() -> None:
    assert "没有发现" in render_text([], limit=None)


def test_render_text_can_show_evidence() -> None:
    assessment = make_assessment(
        target="g1",
        target_kind=TargetKind.GROUP,
        score=70.0,
        hits=[make_hit(signal="coordination.burst", kind=SignalKind.COORDINATION,
                       target="g1", target_kind=TargetKind.GROUP,
                       evidence={"topic": "t1", "count": 12})],
    )
    out = render_text([assessment], limit=None, show_evidence=True)
    assert "t1" in out


# --------------------------------------------------------------------------
# 机读
# --------------------------------------------------------------------------


def test_to_dict_is_json_serialisable() -> None:
    payload = json.dumps([to_dict(a) for a in _sample()], ensure_ascii=False)
    assert json.loads(payload)[0]["target"] == "i1"


def test_to_dict_keeps_promoted_flag() -> None:
    rows = json.loads(render_json(_sample()))
    promoted = next(r for r in rows if r["target"] == "a1")
    assert promoted["promoted"] is True
    assert promoted["level"] == "weak"


def test_to_dict_exposes_breakdown(config) -> None:
    """贡献分明细必须能在机读输出里拿到，否则没法做「哪个信号在起作用」的复盘。

    这里刻意走真实的评分器，而不是手工拼一个 Assessment —— breakdown 是
    评分器负责填的字段，手工构造的对象不会带上它。
    """
    from tidewatch.scoring import Scorer

    scorer = Scorer(config)
    hits = [
        make_hit(signal="text.keyword", kind=SignalKind.TEXT, target="i1",
                 level=Level.STRONG, score=0.9),
        make_hit(signal="account.age", kind=SignalKind.ACCOUNT, target="i1",
                 level=Level.WEAK, score=0.5),
    ]
    assessments = scorer.assess(hits, targets=[(TargetKind.ITEM, "i1")])
    rows = json.loads(render_json(assessments))
    assert rows[0]["breakdown"]
    assert {row["signal"] for row in rows[0]["breakdown"]} == {
        "text.keyword",
        "account.age",
    }
    assert all({"signal", "contribution"} <= set(row) for row in rows[0]["breakdown"])


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------


def test_render_markdown_runs_and_reports_totals() -> None:
    """回归：Markdown 报告曾读取一个已被改名的统计键，一跑就 KeyError。

    这条测试的价值不在断言本身，而在于它**真的把渲染跑了一遍**。
    """
    out = render_markdown(_sample(), meta={"输入": "sample.jsonl"})
    assert out.startswith("# 检测报告")
    assert "判定对象总数：3" in out
    assert "sample.jsonl" in out


def test_render_markdown_mentions_uncovered_count() -> None:
    out = render_markdown(_sample())
    assert "未触发任何信号" in out


def test_render_markdown_renders_hit_table() -> None:
    out = render_markdown(_sample())
    assert "| 层级 | 信号 | 判定 | 依据 |" in out
    assert "`text.keyword`" in out


def test_render_markdown_reports_group_counts() -> None:
    group = make_assessment(
        target="burst:t1",
        target_kind=TargetKind.GROUP,
        score=75.0,
        hits=[make_hit(signal="coordination.burst", kind=SignalKind.COORDINATION,
                       target="burst:t1", target_kind=TargetKind.GROUP)],
    )
    out = render_markdown([group])
    assert "团伙 1" in out
