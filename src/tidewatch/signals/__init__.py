"""信号注册中心。

导入本包即触发所有信号模块的加载，从而完成 @register 注册。

第三方要加自己的信号，只需要：
  1. 写一个模块，继承 ItemSignal / ActorSignal / CoordinationSignal 之一
  2. 打上 @register
  3. 在 configs/default.yaml 补一段配置
  4. 在启动时 import 你的模块（比如在 cli.py 里加一行）

不需要动引擎的任何既有代码。
"""

from tidewatch.signals import account_signals, coordination, text_signals  # noqa: F401
from tidewatch.signals.base import (
    REGISTRY,
    ActorSignal,
    CoordinationSignal,
    ItemSignal,
    Signal,
    build_signals,
    register,
)

__all__ = [
    "ActorSignal",
    "CoordinationSignal",
    "ItemSignal",
    "REGISTRY",
    "Signal",
    "build_signals",
    "register",
]
