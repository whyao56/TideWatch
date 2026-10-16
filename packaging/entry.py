"""打包入口脚本。

为什么不直接把 `src/tidewatch/cli.py` 当入口：
`python -m tidewatch.cli` / 冻结后以 `__main__` 执行时，同一个文件会**同时以
两个名字**存在于 `sys.modules` 里（`__main__` 和 `tidewatch.cli`），
于是模块级状态各存一份。这个项目现在没有模块级状态，但那是运气不是设计 ——
在入口处用一个独立的薄壳，可以让这个坑永远不会出现。

薄壳里**只做 import 再调用**，不写任何逻辑：逻辑写在包里，才测得到。
"""

from __future__ import annotations

import sys

from tidewatch.cli import main

if __name__ == "__main__":
    sys.exit(main())
