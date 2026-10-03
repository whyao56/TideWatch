"""端到端流水线：Dataset -> 三层信号 -> 聚合评分 -> 可解释报告。

流水线本身刻意做得很薄 —— 它只负责编排和计时，不含任何判定逻辑。
判定逻辑全部在 signals/ 里，评分逻辑全部在 scoring.py 里。
这样做的原因是：调一个判定规则不该需要改动流水线，
否则每次实验都会引入无关的代码差异，对照就失去意义了。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from tidewatch.config import Config
from tidewatch.report import summarize
from tidewatch.rules import RuleBook
from tidewatch.scoring import Scorer, propagate_group_members
from tidewatch.signals import Signal, build_signals
from tidewatch.types import Assessment, Dataset, SignalHit, TargetKind

__all__ = ["Pipeline", "Result"]


@dataclass(slots=True)
class Result:
    """一次完整分析的结果。"""

    dataset: Dataset
    hits: list[SignalHit]
    assessments: list[Assessment]
    timings: dict[str, float] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> dict[str, Any]:
        return summarize(self.assessments)

    def flagged(self, level=None) -> list[Assessment]:
        if level is None:
            return [a for a in self.assessments if a.is_flagged]
        return [a for a in self.assessments if a.level.rank >= level.rank]


class Pipeline:
    """编排器。可注入自定义信号集，方便做消融实验。"""

    def __init__(
        self,
        cfg: Config,
        rules: RuleBook | None = None,
        signals: list[Signal] | None = None,
    ) -> None:
        self.cfg = cfg
        self.rules = rules if rules is not None else RuleBook.load(cfg.path("corpus.rules_dir", "rules"))
        self.scorer = Scorer(cfg)
        self.signals = signals if signals is not None else build_signals(cfg, self.rules)

        propagation = cfg.section("scoring.propagation")
        self.propagate_factor = (
            float(propagation.get("factor", 0.6)) if propagation.get("enabled", True) else 0.0
        )

    @classmethod
    def from_config(
        cls,
        path: str | None = None,
        overrides: list[str] | None = None,
    ) -> Pipeline:
        return cls(Config.load(path, overrides))

    # ---------------- 主流程 ----------------

    def run(self, data: Dataset) -> Result:
        timings: dict[str, float] = {}
        hits: list[SignalHit] = []

        for signal in self.signals:
            started = time.perf_counter()
            produced = signal.run(data)
            timings[signal.name] = round((time.perf_counter() - started) * 1000, 2)
            hits.extend(produced)

        started = time.perf_counter()
        labels = self._build_labels(data, hits)
        # 传入全部 item 和 actor 作为「必须评估的目标」，这样未命中任何信号的
        # 条目也会以 CLEAN 出现在结果里，评测时才能算出真正的精确率与召回率。
        targets: list[tuple[TargetKind, str]] = [
            (TargetKind.ITEM, item.item_id) for item in data.items
        ]
        targets.extend((TargetKind.ACTOR, actor_id) for actor_id in data.actors)
        assessments = self.scorer.assess(hits, labels, targets)

        # 团伙 -> 成员的传播。放在评分之后、计时之前，因为它需要团伙的判定结果；
        # 传播改变了账号层的输入，所以账号需要重新评一次分。
        if self.propagate_factor > 0:
            extra = propagate_group_members(assessments, self.propagate_factor)
            if extra:
                # 保留原有的全部命中，只**追加**传播过来的证据。
                # 覆盖掉账号层原有命中的话，一个「老账号 + 资料正常」的
                # 团伙成员会因为它原本没有负面信号而被传播分从零算起。
                merged = list(hits)
                for group_hits in extra.values():
                    merged.extend(group_hits)
                hits = merged
                assessments = self.scorer.assess(hits, labels, targets)

        timings["scoring"] = round((time.perf_counter() - started) * 1000, 2)

        meta = {
            "items": len(data.items),
            "actors": len(data.actors),
            "topics": len(data.by_topic()),
            "source": data.source,
            "config_fingerprint": self.cfg.fingerprint(),
            "rules": self.rules.stats(),
            "signals": [s.name for s in self.signals],
        }
        return Result(data, hits, assessments, timings, meta)

    # ---------------- 内部 ----------------

    def _build_labels(self, data: Dataset, hits: list[SignalHit]) -> dict[str, str]:
        """给每个可判定目标配一个人类可读的标识，让报告不必只显示 id。"""
        labels: dict[str, str] = {}
        for item in data.items:
            labels[item.item_id] = item.preview
        for actor_id, actor in data.actors.items():
            labels[actor_id] = actor.name or actor_id
        for hit in hits:
            if hit.target_kind is TargetKind.GROUP:
                topic = hit.evidence.get("topic")
                labels.setdefault(hit.target, f"{hit.signal} @ {topic}" if topic else hit.target)
        return labels
