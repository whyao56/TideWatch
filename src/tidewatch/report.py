"""报告渲染：把 Assessment 变成人能读、机器能读的东西。

一份判断如果没有解释，使用者就只能选择「信」或「不信」；有了逐条依据，
他才能做出「这条我认，那条我不认」的判断，并且把这些反馈变成下一次
调参数、改规则的输入。所以报告不是附属品，它是迭代闭环的入口。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from tidewatch.types import Assessment, Level

__all__ = [
    "to_dict",
    "render_text",
    "render_json",
    "render_markdown",
    "summarize",
]

_KIND_LABEL = {"text": "文本", "account": "账号", "coordination": "协同"}
_KIND_ORDER = ["text", "account", "coordination"]


# --------------------------------------------------------------------------
# 机读
# --------------------------------------------------------------------------


def to_dict(assessment: Assessment) -> dict[str, Any]:
    return {
        "target": assessment.target,
        "target_kind": assessment.target_kind.value,
        "label": assessment.label,
        "score": assessment.score,
        "level": assessment.level.value,
        "level_label": assessment.level.label,
        "bonus": assessment.bonus,
        "promoted": assessment.promoted,
        "hits": [
            {
                "signal": hit.signal,
                "kind": hit.kind.value,
                "level": hit.level.value,
                "score": round(hit.score, 4),
                "weight": hit.weight,
                "reason": hit.reason,
                "evidence": hit.evidence,
            }
            for hit in assessment.hits
        ],
        "breakdown": [{"signal": s, "contribution": c} for s, c in assessment.breakdown],
    }


def render_json(assessments: Sequence[Assessment], *, indent: int = 2) -> str:
    return json.dumps([to_dict(a) for a in assessments], ensure_ascii=False, indent=indent)


def summarize(assessments: Sequence[Assessment]) -> dict[str, Any]:
    """统计概览。评测脚本和 CLI 都用它，避免两处各写一套口径。

    `no_signal` 是「完全没触发任何信号」的条目数，它和 `clean`
    （触发了但分数没到阈值）是两件事 —— 分开统计才能看出规则是「太严」
    还是「根本没覆盖到」。
    """
    counts = Counter(a.level.value for a in assessments)
    flagged = [a for a in assessments if a.is_flagged]
    by_kind = Counter(a.target_kind.value for a in flagged)
    signals = Counter(hit.signal for a in flagged for hit in a.hits)
    return {
        "assessed": len(assessments),
        "no_signal": sum(1 for a in assessments if not a.hits),
        "clean": counts.get("clean", 0),
        "weak": counts.get("weak", 0),
        "strong": counts.get("strong", 0),
        "flagged": len(flagged),
        "flagged_by_kind": {k: by_kind.get(k, 0) for k in ("item", "actor", "group")},
        "top_signals": signals.most_common(5),
    }


# --------------------------------------------------------------------------
# 人读
# --------------------------------------------------------------------------


def render_text(
    assessments: Sequence[Assessment],
    *,
    limit: int | None = 20,
    min_level: Level | None = None,
    show_evidence: bool = False,
) -> str:
    picked: Iterable[Assessment] = assessments
    if min_level is not None:
        picked = [a for a in picked if a.level.rank >= min_level.rank]
    picked = list(picked)
    if limit is not None:
        picked = picked[:limit]
    if not picked:
        return "没有发现达到阈值的可疑目标。"

    blocks: list[str] = []
    for assessment in picked:
        note = "  ← 分数未达阈值，因命中强信号保底提升" if assessment.promoted else ""
        lines = [
            f"── 【{assessment.level.label}】 {assessment.score:.1f} 分 "
            f"· {assessment.target_kind.value} · {assessment.label or assessment.target}{note}"
        ]
        ordered = sorted(
            assessment.hits,
            key=lambda h: (_KIND_ORDER.index(h.kind.value) if h.kind.value in _KIND_ORDER else 9, -h.score),
        )
        for hit in ordered:
            tag = _KIND_LABEL.get(hit.kind.value, hit.kind.value)
            lines.append(f"   [{tag}] {hit.signal:<24} {hit.reason}")
            if show_evidence and hit.evidence:
                blob = json.dumps(hit.evidence, ensure_ascii=False)
                lines.append(f"          ↳ {blob[:240]}")
        if assessment.bonus:
            lines.append(
                f"   [+] 跨层联动加分 +{assessment.bonus:.1f}"
                f"（命中 {len(assessment.kinds_hit)} 个层级）"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_markdown(
    assessments: Sequence[Assessment],
    *,
    meta: dict[str, Any] | None = None,
    limit: int | None = 50,
) -> str:
    meta = meta or {}
    picked = list(assessments)
    if limit is not None:
        picked = picked[:limit]
    stats = summarize(assessments)

    parts: list[str] = ["# 检测报告\n"]
    if meta:
        parts.append("| 项目 | 值 |")
        parts.append("|---|---|")
        for key, value in meta.items():
            parts.append(f"| {key} | {value} |")
        parts.append("")

    parts.append("## 概览\n")
    parts.append(
        f"- 判定对象总数：{stats['assessed']}"
        f"（正常 {stats['clean']} / 弱嫌疑 {stats['weak']} / 强嫌疑 {stats['strong']}）"
    )
    parts.append(
        f"- 其中 {stats['no_signal']} 个未触发任何信号"
        f"（不是「判定为正常」，而是「现有规则没覆盖」）"
    )
    parts.append(
        f"- 命中分布：单条 {stats['flagged_by_kind']['item']} · "
        f"账号 {stats['flagged_by_kind']['actor']} · "
        f"团伙 {stats['flagged_by_kind']['group']}"
    )
    if stats["top_signals"]:
        parts.append(
            "- 最常触发的信号：" + "、".join(f"`{s}`({c})" for s, c in stats["top_signals"])
        )
    parts.append("")

    parts.append("## 明细\n")
    for assessment in picked:
        parts.append(
            f"### 【{assessment.level.label}】{assessment.score:.1f} 分 · "
            f"{assessment.label or assessment.target}\n"
        )
        parts.append("| 层级 | 信号 | 判定 | 依据 |")
        parts.append("|---|---|---|---|")
        for hit in sorted(assessment.hits, key=lambda h: -h.score):
            tag = _KIND_LABEL.get(hit.kind.value, hit.kind.value)
            parts.append(f"| {tag} | `{hit.signal}` | {hit.level.label} | {hit.reason} |")
        if assessment.bonus:
            parts.append("")
            parts.append(
                f"> 跨层联动加分 **+{assessment.bonus:.1f}**"
                f"（命中 {len(assessment.kinds_hit)} 个层级）"
            )
        parts.append("")
    return "\n".join(parts)
