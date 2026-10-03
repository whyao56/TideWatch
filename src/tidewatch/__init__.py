"""tidewatch —— 网络水军与协同行为识别引擎。

定位：一个**可解释、可插拔、可迭代**的协同行为识别引擎。
喂给它一批发言和账号信息，它给出「哪些内容/账号/团伙可疑、为什么可疑」。

不做的事：不内置爬虫。数据获取交给使用者，引擎只定义标准输入格式。
这样既规避了合规风险，也让任何人都能接自己的数据源。

快速开始::

    from tidewatch import Pipeline, Dataset
    from tidewatch.adapters import MappedAdapter

    data = MappedAdapter().load([{"text": "加薇❤️信详聊", "actor_id": "u1"}])
    result = Pipeline.from_config().run(data)
    for a in result.flagged():
        print(a.level.label, a.score, a.reasons)
"""

from tidewatch.config import Config
from tidewatch.pipeline import Pipeline, Result
from tidewatch.rules import RuleBook
from tidewatch.types import (
    Actor,
    Assessment,
    Dataset,
    Item,
    Level,
    SignalHit,
    SignalKind,
    TargetKind,
)

__version__ = "0.1.0"

__all__ = [
    "Actor",
    "Assessment",
    "Config",
    "Dataset",
    "Item",
    "Level",
    "Pipeline",
    "Result",
    "RuleBook",
    "SignalHit",
    "SignalKind",
    "TargetKind",
    "__version__",
]
