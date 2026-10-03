"""评测脚本的测试。

评测脚本是这个项目最容易被质疑的部分（「你的指标怎么算的」），所以它的
每一处口径都必须可验证：

  - 混淆矩阵的四个格子怎么归
  - 团伙为什么只报纯度、不报 P/R
  - 消融实验的覆盖项是不是真的关掉了对应的层
  - 标注从数据里怎么读出来
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from conftest import make_assessment, make_hit
from tidewatch.rules import RuleBook
from tidewatch.types import Assessment, Level, SignalKind, TargetKind

ROOT = Path(__file__).resolve().parents[1]


def _load_eval_module():
    """按文件路径加载 eval/run_eval.py。

    `eval` 是内置函数名，也不在 src 包下，所以不能靠 import 语句拿到它。
    """
    spec = importlib.util.spec_from_file_location("tidewatch_eval", ROOT / "eval" / "run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ev():
    return _load_eval_module()


@pytest.fixture
def rules() -> RuleBook:
    return RuleBook.load(ROOT / "rules")


LABELLED_RECORDS = [
    {
        "item_id": "n1",
        "actor_id": "real-1",
        "text": "我对这个方案的看法是，得先做一轮灰度再下结论",
        "created_at": 1788000000,
        "topic": "v1",
        "is_synthetic_positive": False,
    },
    {
        "item_id": "p1",
        "actor_id": "fake-1",
        "text": "详情私聊我，加微信谈价格",
        "created_at": 1788000060,
        "topic": "v1",
        "is_synthetic_positive": True,
    },
    {
        "item_id": "p2",
        "actor_id": "fake-1",
        "text": "回复楼上，这个说法站不住脚",
        "created_at": 1788000120,
        "topic": "v1",
        "is_synthetic_positive": True,
    },
]


@pytest.fixture
def labelled_file(tmp_path) -> Path:
    path = tmp_path / "labelled.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in LABELLED_RECORDS), encoding="utf-8"
    )
    return path


# --------------------------------------------------------------------------
# 纯函数
# --------------------------------------------------------------------------


def test_prf_matches_hand_computed_values(ev) -> None:
    assert ev.prf(2, 1, 2) == pytest.approx((2 / 3, 0.5, 4 / 7))


def test_prf_handles_zero_denominators(ev) -> None:
    """没有正例时不能除零，也不该报出「完美的 0」。"""
    assert ev.prf(0, 0, 0) == (0.0, 0.0, 0.0)
    assert ev.prf(0, 3, 0) == (0.0, 0.0, 0.0)


def test_level_distribution_counts_all_three_levels(ev) -> None:
    rows = [
        make_assessment(target="a", target_kind=TargetKind.ITEM, level=Level.CLEAN),
        make_assessment(target="b", target_kind=TargetKind.ITEM, level=Level.WEAK),
        make_assessment(target="c", target_kind=TargetKind.ITEM, level=Level.WEAK),
        make_assessment(target="d", target_kind=TargetKind.ITEM, level=Level.STRONG),
    ]
    assert ev.level_distribution(rows) == {"clean": 1, "weak": 2, "strong": 1}


# --------------------------------------------------------------------------
# 混淆矩阵
# --------------------------------------------------------------------------


def _rows(specs):
    return [
        make_assessment(target=target, target_kind=TargetKind.ITEM, level=level) for target, level in specs
    ]


def test_evaluate_level_confusion_matrix(ev) -> None:
    """四个格子逐个核对，避免「P/R 看起来合理但算错了」这种问题。"""
    rows = _rows(
        [
            ("a", Level.STRONG),  # 真水军 + 判可疑 -> TP
            ("b", Level.CLEAN),   # 真水军 + 判正常 -> FN
            ("c", Level.WEAK),    # 正常人 + 判可疑 -> FP
            ("d", Level.CLEAN),   # 正常人 + 判正常 -> TN
        ]
    )
    labels = {"a": True, "b": True, "c": False, "d": False}
    got = ev.evaluate_level(rows, labels, TargetKind.ITEM, Level.WEAK)
    assert (got["tp"], got["fp"], got["fn"], got["tn"]) == (1, 1, 1, 1)
    assert got["precision"] == pytest.approx(0.5)
    assert got["recall"] == pytest.approx(0.5)


def test_evaluate_level_min_level_shifts_prediction(ev) -> None:
    """同一个「弱嫌疑」条目，在 strong 口径下就该算作「判为正常」。"""
    rows = _rows([("a", Level.WEAK)])
    labels = {"a": True}
    as_weak = ev.evaluate_level(rows, labels, TargetKind.ITEM, Level.WEAK)
    as_strong = ev.evaluate_level(rows, labels, TargetKind.ITEM, Level.STRONG)
    assert (as_weak["tp"], as_weak["fn"]) == (1, 0)
    assert (as_strong["tp"], as_strong["fn"]) == (0, 1)


def test_evaluate_level_ignores_other_target_kinds(ev) -> None:
    """算 item 指标时不能把账号混进来，否则分母是错的。"""
    rows = [
        make_assessment(target="i", target_kind=TargetKind.ITEM, level=Level.STRONG),
        make_assessment(target="a", target_kind=TargetKind.ACTOR, level=Level.STRONG),
    ]
    got = ev.evaluate_level(rows, {"i": True, "a": True}, TargetKind.ITEM, Level.WEAK)
    assert got["tp"] == 1


def test_evaluate_level_treats_unlabelled_as_negative(ev) -> None:
    """标注里没有的目标按「非水军」处理 —— 漏标不该被算成命中。"""
    got = ev.evaluate_level(
        _rows([("mystery", Level.STRONG)]), {}, TargetKind.ITEM, Level.WEAK
    )
    assert got["fp"] == 1


# --------------------------------------------------------------------------
# 团伙纯度
# --------------------------------------------------------------------------


def test_evaluate_groups_reports_purity(ev) -> None:
    """团伙没有标准答案可对，所以看的是「抓出来的东西有多少是真的」。"""
    hit = make_hit(
        signal="coordination.burst",
        kind=SignalKind.COORDINATION,
        target="burst:t1",
        target_kind=TargetKind.GROUP,
        evidence={"actors": ["fake-1", "fake-2", "real-1"]},
    )
    group = make_assessment(target="burst:t1", score=70.0, hits=[hit])
    rows = ev.evaluate_groups([group], {"fake-1": True, "fake-2": True, "real-1": False})
    assert len(rows) == 1
    assert rows[0]["size"] == 3
    assert rows[0]["fake"] == 2
    assert rows[0]["purity"] == pytest.approx(2 / 3, abs=1e-3)  # 报告里保留三位小数


def test_evaluate_groups_skips_clean_groups(ev) -> None:
    hit = make_hit(
        target="cooccur:x",
        target_kind=TargetKind.GROUP,
        kind=SignalKind.COORDINATION,
        evidence={"members": ["a", "b"]},
    )
    clean = make_assessment(target="cooccur:x", score=3.0, level=Level.CLEAN, hits=[hit])
    assert ev.evaluate_groups([clean], {"a": True, "b": True}) == []


def test_evaluate_groups_reads_both_member_keys(ev) -> None:
    """burst 用 actors、cooccur 用 members，评测必须两个都认，否则会漏统计。"""
    burst = make_assessment(
        target="burst:t",
        score=70.0,
        hits=[make_hit(target="burst:t", target_kind=TargetKind.GROUP,
                       kind=SignalKind.COORDINATION, evidence={"actors": ["a"]})],
    )
    cooccur = make_assessment(
        target="cooccur:x",
        score=70.0,
        hits=[make_hit(target="cooccur:x", target_kind=TargetKind.GROUP,
                       kind=SignalKind.COORDINATION, evidence={"members": ["b"]})],
    )
    rows = ev.evaluate_groups([burst, cooccur], {"a": True, "b": True})
    assert {row["target"] for row in rows} == {"burst:t", "cooccur:x"}


def test_evaluate_groups_skips_hits_without_member_list(ev) -> None:
    group = make_assessment(
        target="cluster:t",
        score=70.0,
        hits=[make_hit(target="cluster:t", target_kind=TargetKind.GROUP,
                       kind=SignalKind.COORDINATION, evidence={"size": 3})],
    )
    assert ev.evaluate_groups([group], {}) == []


# --------------------------------------------------------------------------
# 消融配置
# --------------------------------------------------------------------------


def test_disable_targets_only_the_requested_kind(ev, rules) -> None:
    """关掉一层必须只关那一层。

    覆盖项生成错了的话，消融实验会静默变成「关掉全部」或「什么都没关」，
    而表格里的数字照样打印出来 —— 结论就全假了。
    """
    overrides = ev.disable("coordination")
    assert overrides
    assert all(o.startswith("signals.coordination.") for o in overrides)
    assert all(o.endswith(".enabled=false") for o in overrides)
    assert len(overrides) == 3  # burst / cluster / cooccur


def test_ablations_cover_every_layer_combination(ev) -> None:
    names = [name for name, _ in ev.ABLATIONS]
    assert names == [
        "全量",
        "仅文本层",
        "仅账号层",
        "仅协同层",
        "文本+账号",
        "文本+协同",
        "账号+协同",
    ]


def test_disable_actually_removes_signals(ev, rules, config) -> None:
    """把参与消融的每个层级都真的跑一遍构造，确认信号数确实减少了。"""
    from tidewatch.signals import build_signals

    full = len(build_signals(config, rules))
    for kind in ("text", "account", "coordination"):
        off = config.with_overrides(*ev.disable(kind))
        assert len(build_signals(off, rules)) < full
        assert len(build_signals(off, rules)) > 0


# --------------------------------------------------------------------------
# 标注读取
# --------------------------------------------------------------------------


def test_load_labelled_reads_items_and_actors(ev, labelled_file) -> None:
    data, item_label, actor_label = ev.load_labelled(labelled_file)
    assert len(data.items) == 3
    assert item_label == {"n1": False, "p1": True, "p2": True}
    # 只要发过一条水军内容，这个账号就算水军账号
    assert actor_label["fake-1"] is True
    assert actor_label["real-1"] is False


def test_load_labelled_marks_actor_by_prefix_even_without_positive_flags(ev, tmp_path) -> None:
    """账号标注同时看 `fake-` 前缀和逐条标注 —— 防止只标了两种中的一种就漏判。"""
    path = tmp_path / "prefix.jsonl"
    path.write_text(
        json.dumps({"item_id": "x", "actor_id": "fake-9", "text": "内容"}, ensure_ascii=False),
        encoding="utf-8",
    )
    _, item_label, actor_label = ev.load_labelled(path)
    assert item_label == {}   # 没有 is_synthetic_positive，不硬猜
    assert actor_label["fake-9"] is True


def test_load_labelled_skips_records_without_annotation(ev, tmp_path) -> None:
    path = tmp_path / "partial.jsonl"
    path.write_text(
        json.dumps({"item_id": "x", "actor_id": "u", "text": "内容"}, ensure_ascii=False),
        encoding="utf-8",
    )
    data, item_label, actor_label = ev.load_labelled(path)
    assert len(data.items) == 1
    assert item_label == {}
    assert actor_label == {"u": False}


# --------------------------------------------------------------------------
# 端到端：跑一组消融
# --------------------------------------------------------------------------


def test_run_ablation_produces_metrics(ev, rules, labelled_file) -> None:
    """真的跑一次消融，确认指标结构齐全、数字在合法区间内。"""
    ev._RULES = rules
    data, item_label, actor_label = ev.load_labelled(labelled_file)
    row = ev.run_ablation("全量", [], data, item_label, actor_label, None, Level.WEAK)

    assert row["name"] == "全量"
    assert row["signals"] > 0
    for key in ("item", "actor"):
        metrics = row[key]
        assert set(metrics) == {"tp", "fp", "fn", "tn", "precision", "recall", "f1"}
        for name in ("precision", "recall", "f1"):
            assert 0.0 <= metrics[name] <= 1.0
    assert row["latency_ms"] >= 0
    assert sum(row["levels"].values()) == metrics_total(row)


def metrics_total(row) -> int:
    return row["levels"]["clean"] + row["levels"]["weak"] + row["levels"]["strong"]


def test_run_ablation_disabling_a_layer_changes_outcome(ev, rules, labelled_file) -> None:
    """关掉协同层后，团伙必然为零 —— 这是消融结论可信的前提。"""
    ev._RULES = rules
    data, item_label, actor_label = ev.load_labelled(labelled_file)
    full = ev.run_ablation("全量", [], data, item_label, actor_label, None, Level.WEAK)
    only_text = ev.run_ablation(
        "仅文本层", ev.disable("account", "coordination"), data, item_label, actor_label,
        None, Level.WEAK,
    )
    assert only_text["signals"] < full["signals"]
    assert only_text["groups"] == []


def test_render_markdown_contains_all_sections(ev) -> None:
    row = {
        "name": "全量",
        "signals": 10,
        "item": {"tp": 1, "fp": 0, "fn": 1, "tn": 8, "precision": 1.0, "recall": 0.5, "f1": 0.667},
        "actor": {"tp": 1, "fp": 0, "fn": 0, "tn": 5, "precision": 1.0, "recall": 1.0, "f1": 1.0},
        "groups": [{"signal": "coordination.burst", "target": "burst:t", "size": 3,
                    "fake": 3, "purity": 1.0}],
        "levels": {"clean": 8, "weak": 1, "strong": 1},
        "latency_ms": 12.3,
        "fingerprint": "abc123",
    }
    out = ev.render_markdown([row], meta={"数据文件": "x.jsonl"}, min_level=Level.WEAK)
    assert out.startswith("# 检测效果评测")
    assert "## 实验设置" in out
    assert "## 主对比" in out
    assert "## 混淆矩阵" in out
    assert "## 团伙检测纯度" in out
    assert "## 判定分布" in out
    assert "| 全量 | 10 |" in out


def test_render_markdown_handles_group_less_rows(ev) -> None:
    row = {
        "name": "仅文本层",
        "signals": 4,
        "item": {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "precision": 0.0, "recall": 0.0, "f1": 0.0},
        "actor": {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "precision": 0.0, "recall": 0.0, "f1": 0.0},
        "groups": [],
        "levels": {"clean": 0, "weak": 0, "strong": 0},
        "latency_ms": 1.0,
        "fingerprint": "x",
    }
    out = ev.render_markdown([row], meta={}, min_level=Level.WEAK)
    assert "| 仅文本层 | 0 | - | - |" in out


def test_assessment_type_is_usable_by_eval(ev) -> None:
    """评测只依赖 Assessment 的几个公开属性，这条测试把这份契约固定住。"""
    assessment = Assessment(
        target="t", target_kind=TargetKind.GROUP, score=1.0, level=Level.WEAK
    )
    assert assessment.is_flagged is True
    assert assessment.target_kind is TargetKind.GROUP
