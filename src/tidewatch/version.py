"""版本号的**唯一**来源。

打包之后最难对齐的就是版本号：`pyproject.toml`、包内常量、exe 所在的文件夹名、
GitHub Release 的 tag —— 任何一处不一致，使用者看到的现象都是
「检查更新说有新版，装完还是旧的」，而这种情况极难从现象反推原因。

所以这里只存一份，其余地方全部从它派生，并且有测试盯着
`pyproject.toml` 与 `configs/default.yaml` 必须和它一致（见 `tests/test_version.py`）。
"""

from __future__ import annotations

__version__ = "0.0.1"
