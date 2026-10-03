"""评分聚合：把三层信号合成一个可决策的分数与档位。

公式::

    score = Σ(强度 × 档位倍率 × 信号权重 × 层权重) × scale + 联动加分
    封顶到 cap，再按 levels 切成三档

三个刻意的设计，每一个都对应一类常见失败：

1. **档位倍率** —— 同一个信号判 STRONG 比判 WEAK 贡献更多分。
   否则「信号自己很确信」这件事传不到总分上，强证据会被平均掉。

2. **联动加分只在跨层时生效** —— 同一层里命中十个信号不加分，因为那通常
   只是同一个原因的十种说法，不构成独立证据。跨层命中才意味着
   「不同类型的数据都指向同一个结论」。

3. **保底机制** —— 有 STRONG 信号却算出「正常」时，提升为「弱嫌疑」。
   宁可让人多看一眼，也不要让明确的证据被算术平均掉。
"""

from __future__ import annotations

from collections import defaultdict

from tidewatch.config import Config
from tidewatch.types import Assessment, Level, SignalHit, SignalKind, TargetKind

__all__ = ["Scorer", "propagate_group_members"]


def propagate_group_members(
    assessments: list[Assessment],
    factor: float,
) -> dict[str, list[SignalHit]]:
    """把团伙的可疑度按比例传给它的成员账号。

    为什么需要这一步：协同层产出的是「团伙」这个整体，但使用者看完报告后
    第一个问题必然是「那具体是哪些账号」。不传播的话，一个完全由水军组成的
    团伙被挖出来了，成员账号在结果里却一行都看不到 —— 结论是对的，但不可用。

    传播是有代价的（团伙误报会连坐成员），所以：
      - 系数默认只有 0.6，成员拿不到团伙的全部分数
      - 传播过去的信号一律标 WEAK，不直接定案
      - 只从**已判为可疑**的团伙往外传
    """
    extra: dict[str, list[SignalHit]] = defaultdict(list)

    for assessment in assessments:
        if assessment.target_kind is not TargetKind.GROUP or not assessment.is_flagged:
            continue
        for hit in assessment.hits:
            members = list(hit.evidence.get("actors") or hit.evidence.get("members") or [])
            if not members:
                continue
            for member in members:
                extra[member].append(
                    SignalHit(
                        signal=f"{hit.signal}#member",
                        kind=SignalKind.COORDINATION,
                        target=member,
                        target_kind=TargetKind.ACTOR,
                        level=Level.WEAK,
                        score=hit.score * factor,
                        weight=hit.weight,
                        reason=(
                            f"出现在可疑团伙中"
                            f"（{hit.signal}，团伙分 {assessment.score:.0f}）"
                        ),
                        evidence={
                            "group": assessment.target,
                            "group_score": assessment.score,
                            "via": hit.signal,
                        },
                    )
                )
    return dict(extra)


class Scorer:
    """把 SignalHit 聚合成 Assessment。"""

    def __init__(self, cfg: Config) -> None:
        section = cfg.section("scoring")
        self.kind_weights: dict[str, float] = {
            str(k): float(v) for k, v in (section.get("kind_weights") or {}).items()
        }
        self.level_multiplier: dict[str, float] = {
            str(k): float(v) for k, v in (section.get("level_multiplier") or {}).items()
        }
        synergy = section.get("synergy") or {}
        self.min_kinds = int(synergy.get("min_kinds", 2))
        self.bonus_per_extra = float(synergy.get("bonus_per_extra", 8.0))

        levels = section.get("levels") or {}
        self.weak_threshold = float(levels.get("weak", 20))
        self.strong_threshold = float(levels.get("strong", 50))

        self.scale = float(section.get("scale", 25.0))
        self.cap = float(section.get("cap", 100))

    # ---------------- 主流程 ----------------

    def assess(
        self,
        hits: list[SignalHit],
        labels: dict[str, str] | None = None,
        targets: list[tuple[TargetKind, str]] | None = None,
    ) -> list[Assessment]:
        """把一串 SignalHit 按目标归组并评分，按分数降序返回。

        `targets` 是「必须出现在结果里的目标清单」。不传的话，未命中任何
        信号的条目会从结果中消失 —— 那样评测时就无法区分「判定为正常」和
        「根本没跑过」，这两件事必须能分开，否则算不出召回率。
        """
        labels = labels or {}
        grouped: dict[tuple[TargetKind, str], list[SignalHit]] = defaultdict(list)
        for hit in hits:
            grouped[(hit.target_kind, hit.target)].append(hit)

        if targets is not None:
            for kind, target in targets:
                grouped.setdefault((kind, target), [])

        results = [
            self._assess_one(target_kind, target, group, labels.get(target, ""))
            for (target_kind, target), group in grouped.items()
        ]
        results.sort(key=lambda a: (-a.score, a.target))
        return results

    def _assess_one(
        self,
        target_kind: TargetKind,
        target: str,
        hits: list[SignalHit],
        label: str,
    ) -> Assessment:
        raw = 0.0
        breakdown: list[tuple[str, float]] = []
        for hit in hits:
            kind_weight = self.kind_weights.get(hit.kind.value, 1.0)
            level_mult = self.level_multiplier.get(hit.level.value, 1.0)
            contribution = hit.score * level_mult * hit.weight * kind_weight
            raw += contribution
            breakdown.append((hit.signal, round(contribution * self.scale, 2)))

        score = raw * self.scale

        kinds = {h.kind.value for h in hits}
        bonus = 0.0
        if len(kinds) >= self.min_kinds:
            bonus = (len(kinds) - self.min_kinds + 1) * self.bonus_per_extra
        score = min(score + bonus, self.cap)

        level = self._level_for(score)
        promoted = False
        if level is Level.CLEAN and any(h.level is Level.STRONG for h in hits):
            # 有强信号却算成「正常」时保底提升一档。
            # 分数不变，只提档 —— 所以报告里必须把这面旗标显示出来，
            # 否则使用者会看到「10.5 分 · 弱嫌疑」这种分数与档位对不上的组合。
            level = Level.WEAK
            promoted = True

        return Assessment(
            target=target,
            target_kind=target_kind,
            score=round(score, 2),
            level=level,
            hits=sorted(hits, key=lambda h: (-h.score, h.signal)),
            bonus=round(bonus, 2),
            breakdown=breakdown,
            promoted=promoted,
            label=label,
        )

    def _level_for(self, score: float) -> Level:
        if score >= self.strong_threshold:
            return Level.STRONG
        if score >= self.weak_threshold:
            return Level.WEAK
        return Level.CLEAN
