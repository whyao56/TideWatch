"""信号插件基类与注册机制。

整个引擎的扩展点就在这一个文件里。想加一个新信号：
  1. 继承三个基类之一（按目标粒度：单条 / 账号 / 团伙）
  2. 打上 @register
  3. 在 configs/default.yaml 里加一段配置

就完了。不需要动聚合器、报告、流水线、适配器里的任何一行。

这是「可插拔」在代码层的落地：rules/ 管**数据**（话术、折叠表），
这个注册表管**逻辑**（怎么判断）。两条扩展路径都对外敞开着。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from tidewatch.config import Config
from tidewatch.rules import RuleBook
from tidewatch.types import Actor, Dataset, Item, Level, SignalHit, SignalKind, TargetKind
from tidewatch.utils import clamp

__all__ = [
    "Signal",
    "ItemSignal",
    "ActorSignal",
    "CoordinationSignal",
    "REGISTRY",
    "register",
    "build_signals",
]


REGISTRY: dict[str, type[Signal]] = {}


def register(cls: type[Signal]) -> type[Signal]:
    """把信号注册进全局表。信号名字必须和 configs 里的键一致。"""
    if not getattr(cls, "name", ""):
        raise ValueError(f"{cls.__name__} 缺少 name 类属性")
    if cls.name in REGISTRY:
        raise ValueError(f"信号名重复: {cls.name}")
    REGISTRY[cls.name] = cls
    return cls


class Signal(ABC):
    """所有信号的共同父类。

    一条重要的职责边界：**信号只回答「我看到了什么、有多可疑」，
    不回答「总共多少分」**。所以 `score` 是 [0,1] 的强度，不是最终分数。
    加权、跨层联动、封顶全部由聚合器负责。

    这条边界让加新信号永远不会扰动已有信号的表现 —— 否则调一个信号
    的绝对分值就会连带影响所有实验结论，对照实验就没法做了。
    """

    name: ClassVar[str] = ""
    kind: ClassVar[SignalKind]
    target_kind: ClassVar[TargetKind]
    description: ClassVar[str] = ""
    principle: ClassVar[str] = ""

    def __init__(self, cfg: dict[str, Any], rules: RuleBook) -> None:
        self.cfg = cfg
        self.rules = rules
        self.weight = float(cfg.get("weight", 1.0))

    # ---------------- 生命周期 ----------------

    @abstractmethod
    def run(self, data: Dataset) -> list[SignalHit]:
        """对整批数据执行，返回所有命中。"""

    # ---------------- 工具 ----------------

    def make_hit(
        self,
        target: str,
        level: Level,
        intensity: float,
        reason: str,
        evidence: dict[str, Any] | None = None,
    ) -> SignalHit:
        return SignalHit(
            signal=self.name,
            kind=self.kind,
            target=target,
            target_kind=self.target_kind,
            level=level,
            score=clamp(intensity, 0.0, 1.0),
            reason=reason,
            weight=self.weight,
            evidence=evidence or {},
        )

    @classmethod
    def describe(cls) -> dict[str, str]:
        return {
            "name": cls.name,
            "kind": cls.kind.value,
            "target": cls.target_kind.value,
            "description": cls.description,
            "principle": cls.principle,
        }


class ItemSignal(Signal):
    """逐条判定型（文本层）。基类负责遍历，子类只管单条。"""

    kind = SignalKind.TEXT
    target_kind = TargetKind.ITEM

    def run(self, data: Dataset) -> list[SignalHit]:
        out: list[SignalHit] = []
        for item in data.items:
            out.extend(self.evaluate(item, data))
        return out

    @abstractmethod
    def evaluate(self, item: Item, data: Dataset) -> list[SignalHit]:
        """判定单条发言。"""


class ActorSignal(Signal):
    """账号判定型（账号层）。基类负责按账号聚合发言，子类只管单个账号。"""

    kind = SignalKind.ACCOUNT
    target_kind = TargetKind.ACTOR

    def run(self, data: Dataset) -> list[SignalHit]:
        grouped = data.by_actor()
        out: list[SignalHit] = []
        for actor_id, actor in data.actors.items():
            out.extend(self.evaluate(actor, grouped.get(actor_id, []), data))
        return out

    @abstractmethod
    def evaluate(self, actor: Actor, items: list[Item], data: Dataset) -> list[SignalHit]:
        """判定单个账号。items 是它在本批数据里的全部发言。"""


class CoordinationSignal(Signal):
    """批量判定型（协同层）。必须在整批数据上才有意义。

    与另外两层的本质区别：它不关心「单独看这个像不像」，只关心
    「这群人之间是不是太整齐了」。所以它产出的 target 是团伙（GROUP）。
    """

    kind = SignalKind.COORDINATION
    target_kind = TargetKind.GROUP

    @abstractmethod
    def run(self, data: Dataset) -> list[SignalHit]:
        ...


def build_signals(cfg: Config, rules: RuleBook) -> list[Signal]:
    """按配置实例化所有启用的信号。"""
    out: list[Signal] = []
    for name, cls in sorted(REGISTRY.items()):
        sc = cfg.signal_cfg(name)
        if not sc.get("enabled", True):
            continue
        out.append(cls(sc, rules))
    return out
