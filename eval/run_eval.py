"""评测：在带标注的数据上量化引擎表现，并跑消融实验。

三个层面的指标，因为它们的意义完全不同：
  item 级  —— 逐条发言判得准不准
  actor 级 —— 账号判得准不准（实际使用时最关心的就是这个）
  group 级 —— 能不能把水军团伙整体挖出来

为什么必须分三层：水军的**内容层**常常是正常的（他们也会发正常内容来养号），
真正暴露他们的是协同行为。只看 item 级会严重低估引擎的价值；
只看 group 级又会把误报的代价藏起来。

消融实验的设计意图：每条结论都要能归因到具体的层。说不清「是哪个信号
起了作用」的评测数字，写进简历也经不起追问。

用法::

    python eval/run_eval.py                       # 全量消融
    python eval/run_eval.py --only 全量            # 只跑一组
    python eval/run_eval.py --min-level weak      # 改判定口径
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tidewatch.adapters import MappedAdapter  # noqa: E402
from tidewatch.config import Config  # noqa: E402
from tidewatch.io import read_jsonl  # noqa: E402
from tidewatch.pipeline import Pipeline  # noqa: E402
from tidewatch.rules import RuleBook  # noqa: E402
from tidewatch.signals import REGISTRY, build_signals  # noqa: E402
from tidewatch.types import Assessment, Level, TargetKind  # noqa: E402

DEFAULT_DATA = ROOT / "data" / "eval" / "synthetic_labeled.jsonl"
DEFAULT_OUT = ROOT / "docs" / "benchmark.md"

# 合成数据里水军账号的命名约定。换成真实数据时，把标注换成你自己的字段即可 ——
# 评测脚本只依赖「目标 id -> 是否水军」这一个映射。
FAKE_PREFIX = "fake-"


def disable(*kinds: str) -> list[str]:
    """生成关闭指定层级所有信号的配置覆盖项。"""
    out: list[str] = []
    for name in REGISTRY:
        if name.split(".")[0] in kinds:
            out.append(f"signals.{name}.enabled=false")
    return out


ABLATIONS: list[tuple[str, list[str]]] = [
    ("全量", []),
    ("仅文本层", disable("account", "coordination")),
    ("仅账号层", disable("text", "coordination")),
    ("仅协同层", disable("text", "account")),
    ("文本+账号", disable("coordination")),
    ("文本+协同", disable("account")),
    ("账号+协同", disable("text")),
]


# --------------------------------------------------------------------------
# 数据与标注
# --------------------------------------------------------------------------


def load_labelled(path: Path) -> tuple[Any, dict[str, bool], dict[str, bool]]:
    """读取带标注的数据，返回 (Dataset, item 标注, actor 标注)。

    标注只需要两样：目标 id 和「它是不是水军」。任何数据集只要能提供这个
    映射，就能套进这套评测 —— 这是它不绑定特定数据集的关键。
    """
    records = list(read_jsonl(path))
    data = MappedAdapter().load(records)

    item_label: dict[str, bool] = {}
    actor_label: dict[str, bool] = {}
    for record in records:
        positive = record.get("is_synthetic_positive")
        raw_id = record.get("item_id")
        if raw_id is not None and positive is not None:
            item_label[str(raw_id)] = bool(positive)
        raw_actor = record.get("actor_id")
        if raw_actor is not None:
            aid = str(raw_actor)
            # 一个账号只要发过一条水军内容，就算水军账号
            actor_label[aid] = actor_label.get(aid, False) or aid.startswith(FAKE_PREFIX)
    return data, item_label, actor_label


# --------------------------------------------------------------------------
# 指标
# --------------------------------------------------------------------------


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def evaluate_level(
    assessments: list[Assessment],
    labels: dict[str, bool],
    kind: TargetKind,
    min_level: Level,
) -> dict[str, Any]:
    """在某一层级上算混淆矩阵与 P/R/F1。"""
    tp = fp = fn = tn = 0
    for assessment in assessments:
        if assessment.target_kind is not kind:
            continue
        gold = labels.get(assessment.target, False)
        pred = assessment.level.rank >= min_level.rank
        if gold and pred:
            tp += 1
        elif gold:
            fn += 1
        elif pred:
            fp += 1
        else:
            tn += 1

    precision, recall, f1 = prf(tp, fp, fn)
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def evaluate_groups(
    assessments: list[Assessment],
    actor_label: dict[str, bool],
) -> list[dict[str, Any]]:
    """团伙级：报告每个被判可疑的团伙里，水军账号占了多少。

    团伙没有「标准答案」可对（协同的边界本来就是模糊的），所以这里不做
    P/R，只看**纯度** —— 报出来的团伙里有多少成员确实是水军。
    纯度低说明规则在乱抓，纯度高说明它抓到的东西有意义。
    """
    rows: list[dict[str, Any]] = []
    for assessment in assessments:
        if assessment.target_kind is not TargetKind.GROUP or not assessment.is_flagged:
            continue
        for hit in assessment.hits:
            members = list(hit.evidence.get("actors") or hit.evidence.get("members") or [])
            if not members:
                continue
            actual = [m for m in members if actor_label.get(m, False)]
            purity = len(actual) / len(members) if members else 0.0
            rows.append(
                {
                    "signal": hit.signal,
                    "target": assessment.target,
                    "size": len(members),
                    "fake": len(actual),
                    "purity": round(purity, 3),
                }
            )
    return rows


def level_distribution(assessments: list[Assessment]) -> dict[str, int]:
    counts = Counter(a.level.value for a in assessments)
    return {k: counts.get(k, 0) for k in ("clean", "weak", "strong")}


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def run_ablation(
    name: str,
    overrides: list[str],
    data: Any,
    item_label: dict[str, bool],
    actor_label: dict[str, bool],
    cfg_path: str | None,
    min_level: Level,
) -> dict[str, Any]:
    cfg = Config.load(cfg_path, overrides)
    # 规则库只加载一次，在所有消融组之间共享 —— 它在实验里是不变量，
    # 反复加载只会拖慢速度，还可能因为缓存导致组间不一致。
    pipeline = Pipeline(cfg, rules=_RULES, signals=build_signals(cfg, _RULES))

    started = time.perf_counter()
    result = pipeline.run(data)
    elapsed = (time.perf_counter() - started) * 1000

    return {
        "name": name,
        "signals": len(pipeline.signals),
        "item": evaluate_level(result.assessments, item_label, TargetKind.ITEM, min_level),
        "actor": evaluate_level(result.assessments, actor_label, TargetKind.ACTOR, min_level),
        "groups": evaluate_groups(result.assessments, actor_label),
        "levels": level_distribution(result.assessments),
        "latency_ms": round(elapsed, 1),
        "fingerprint": cfg.fingerprint(),
    }


def render_markdown(
    rows: list[dict[str, Any]],
    *,
    meta: dict[str, Any],
    min_level: Level,
) -> str:
    parts: list[str] = ["# 检测效果评测\n"]
    parts.append("> 本文件由 `eval/run_eval.py` 自动生成，请勿手工编辑。\n")

    parts.append("## 实验设置\n")
    parts.append("| 项目 | 值 |")
    parts.append("|---|---|")
    for key, value in meta.items():
        parts.append(f"| {key} | {value} |")
    parts.append("")

    parts.append(f"## 主对比（判定口径：{min_level.label} 及以上）\n")
    parts.append("| 实验 | 启用信号 | item P | item R | item F1 | actor P | actor R | actor F1 | 耗时 |")
    parts.append("|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        parts.append(
            "| {name} | {signals} | {ip:.3f} | {ir:.3f} | {if1:.3f} | "
            "{ap:.3f} | {ar:.3f} | {af1:.3f} | {lat}ms |".format(
                name=row["name"],
                signals=row["signals"],
                ip=row["item"]["precision"],
                ir=row["item"]["recall"],
                if1=row["item"]["f1"],
                ap=row["actor"]["precision"],
                ar=row["actor"]["recall"],
                af1=row["actor"]["f1"],
                lat=row["latency_ms"],
            )
        )
    parts.append("")

    parts.append("## 混淆矩阵（仅账号层）\n")
    parts.append("| 实验 | TP | FP | FN | TN |")
    parts.append("|---|---|---|---|---|")
    for row in rows:
        a = row["actor"]
        parts.append(f"| {row['name']} | {a['tp']} | {a['fp']} | {a['fn']} | {a['tn']} |")
    parts.append("")

    parts.append("## 团伙检测纯度\n")
    parts.append("> 「纯度」= 被判可疑的团伙成员里，确实是水军的比例。")
    parts.append("> 团伙边界本来就是模糊的，所以这里不报 P/R，只看抓出来的东西有没有意义。\n")
    parts.append("| 实验 | 团伙数 | 平均纯度 | 明细 |")
    parts.append("|---|---|---|---|")
    for row in rows:
        groups = row["groups"]
        if not groups:
            parts.append(f"| {row['name']} | 0 | - | - |")
            continue
        avg = sum(g["purity"] for g in groups) / len(groups)
        detail = "；".join(
            f"`{g['signal'].split('.')[-1]}` {g['fake']}/{g['size']}" for g in groups[:6]
        )
        parts.append(f"| {row['name']} | {len(groups)} | {avg:.3f} | {detail} |")
    parts.append("")

    parts.append("## 判定分布\n")
    parts.append("> 统计的是**全部可判定目标**（含完全没有触发任何信号的），")
    parts.append("> 所以 clean 数量远大于嫌疑数量是正常的。\n")
    parts.append("| 实验 | 正常 | 弱嫌疑 | 强嫌疑 |")
    parts.append("|---|---|---|---|")
    for row in rows:
        lv = row["levels"]
        parts.append(f"| {row['name']} | {lv['clean']} | {lv['weak']} | {lv['strong']} |")
    parts.append("")
    return "\n".join(parts)


_RULES: RuleBook


def main(argv: list[str] | None = None) -> int:
    global _RULES

    parser = argparse.ArgumentParser(description="tidewatch 评测")
    parser.add_argument("--data", default=str(DEFAULT_DATA), help="带标注的数据文件")
    parser.add_argument("--config", default=None, help="配置文件")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="报告输出路径")
    parser.add_argument("--only", default=None, help="只跑指定名称的实验")
    parser.add_argument(
        "--min-level",
        choices=["weak", "strong"],
        default="weak",
        help="判定为「可疑」的最低档位",
    )
    args = parser.parse_args(argv)

    data_path = Path(args.data)
    if not data_path.exists():
        print(f"[error] 找不到数据文件 {data_path}", file=sys.stderr)
        print("        先运行 python scripts/gen_sample.py 生成合成数据", file=sys.stderr)
        return 2

    _RULES = RuleBook.load(ROOT / "rules")
    data, item_label, actor_label = load_labelled(data_path)
    min_level = Level.WEAK if args.min_level == "weak" else Level.STRONG

    print(f"数据：{data_path.name}  {len(data.items)} 条发言 / {len(data.actors)} 个账号")
    print(f"标注：水军发言 {sum(item_label.values())} 条 / 水军账号 {sum(actor_label.values())} 个")
    print(f"口径：{min_level.label} 及以上算可疑\n")

    rows: list[dict[str, Any]] = []
    for name, overrides in ABLATIONS:
        if args.only and args.only != name:
            continue
        row = run_ablation(name, overrides, data, item_label, actor_label, args.config, min_level)
        rows.append(row)
        a = row["actor"]
        i = row["item"]
        print(
            f"  {name:<10} 信号{row['signals']:>2}  "
            f"item P/R/F1 {i['precision']:.3f}/{i['recall']:.3f}/{i['f1']:.3f}  "
            f"actor P/R/F1 {a['precision']:.3f}/{a['recall']:.3f}/{a['f1']:.3f}  "
            f"团伙{len(row['groups'])}  {row['latency_ms']}ms"
        )

    if not rows:
        print("[error] 没有匹配的实验", file=sys.stderr)
        return 2

    meta = {
        "数据文件": data_path.name,
        "发言数": len(data.items),
        "账号数": len(data.actors),
        "话题数": len(data.by_topic()),
        "水军发言数": sum(item_label.values()),
        "水军账号数": sum(actor_label.values()),
        "判定口径": min_level.label,
        "生成时间": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(rows, meta=meta, min_level=min_level), encoding="utf-8")
    print(f"\n报告已写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
