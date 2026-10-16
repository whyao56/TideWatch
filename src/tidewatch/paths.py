"""资源路径解析 —— 让「源码运行」和「打包成 exe」走同一套查找逻辑。

打包最容易出的一类问题不是崩溃，而是**路径悄悄变了**：
源码里 `configs/default.yaml` 是相对仓库根的一个普通文件，
冻结之后 `__file__` 指向一个解压出来的临时目录，同一个表达式就会找不到东西。

所以判断只在这一个模块里做，其它模块不感知「自己是不是在 exe 里」。
三条规则：

  1. **只读的默认资源随包分发** —— `configs/` `rules/` 放在 exe 同级目录，
     使用者能直接看到、直接改（这正是这个项目「改 YAML 就能微调」的前提）。
  2. **包内目录不可用时退回用户目录** —— exe 被放在 `C:\\Program Files` 这类
     只读位置时，把默认资源首次运行复制到 `%LOCALAPPDATA%\\TideWatch`。
  3. **可写的产物进用户目录** —— 报告、评测输出统一落在用户目录下。

查找顺序（先命中的胜出）：

    TIDEWATCH_HOME 环境变量  →  exe 同级目录 / 仓库根  →  包内 _MEIPASS  →  用户目录

其中 `TIDEWATCH_HOME` 是给「我就是要用自己那一套规则」准备的逃生口，
优先级最高，设了就一定用它。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
from pathlib import Path

__all__ = [
    "APP_NAME",
    "app_dir",
    "bundled_dir",
    "describe_paths",
    "is_frozen",
    "is_writable",
    "resource_root",
    "search_locations",
    "seed_user_root",
    "user_root",
]

APP_NAME = "TideWatch"

# 需要随包分发、并且希望使用者能直接改的目录。
RESOURCE_DIRS = ("configs", "rules", "data/sample")


def is_frozen() -> bool:
    """当前是否运行在打包产物里。

    PyInstaller 会往 `sys` 上挂 `frozen`，这是它官方文档认可的判据；
    `sys._MEIPASS` 只作兜底，因为那是实现细节，别的打包器不一定有。
    """
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def app_dir() -> Path:
    """程序所在目录 —— 冻结态是 exe 的父目录，源码态是仓库根。

    冻结态刻意用 `sys.executable` 而不是 `__file__`：
    `__file__` 在 onedir 模式下指向 `_internal/tidewatch/...`，
    我们要的是用户能看到的那一层目录。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    # src/tidewatch/paths.py -> src/tidewatch -> src -> 仓库根
    return Path(__file__).resolve().parents[2]


def bundled_dir() -> Path | None:
    """包内解压目录（PyInstaller 的 `_MEIPASS`）。源码态返回 None。"""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else None


def user_root() -> Path:
    """可写的用户数据根目录。

    Windows 用 `%LOCALAPPDATA%\\TideWatch`；其它平台按 XDG 约定退到 `~/.local/share`。
    允许用 `TIDEWATCH_HOME` 整体改写 —— 测试和「绿色版」都要能把它指到别处。
    """
    override = os.environ.get("TIDEWATCH_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / APP_NAME


def search_locations() -> list[tuple[str, Path]]:
    """按优先级列出所有会被查找的资源根，附带人类可读的来源名。

    自检（`tidewatch doctor`）会把它整张表打出来 ——
    排错时最想知道的就是「你到底去哪里找了」。
    """
    found: list[tuple[str, Path]] = []
    override = os.environ.get("TIDEWATCH_HOME")
    if override:
        found.append(("环境变量 TIDEWATCH_HOME", Path(override).expanduser()))
    found.append(("程序目录（exe 同级）" if is_frozen() else "仓库根", app_dir()))
    bundled = bundled_dir()
    if bundled is not None:
        found.append(("包内解压目录", bundled))
    found.append(("用户目录", user_root()))
    return found


def _looks_like_resource_root(root: Path) -> bool:
    """这个目录是不是「我这一份资源」。

    判据刻意放宽到「`configs/` 或 `rules/` 存在」，而不是「`configs/default.yaml` 存在」。
    原因是两种失败方式的好坏完全不同：

    - 目录**完全不在**（例如使用者只解压了 exe）→ 应该往下一级找兜底副本；
    - 目录**在但不完整**（少了一个规则文件、配置文件被改名）→ 必须**停在这里**，
      由自检大声报出来。否则引擎会安静地切到 `_internal` 里那一份旧副本，
      使用者改过的规则明明生效不了、却一切正常 —— 这正是最难查的那种错。
    """
    try:
        return (root / "configs").is_dir() or (root / "rules").is_dir()
    except OSError:  # 权限问题、路径过长等，一律当成「不是」
        return False


def is_writable(root: Path) -> bool:
    """探测目录可写性。不依赖 `os.access` —— 它在 Windows 上对 ACL 判断不准。"""
    if not root.exists():
        return False
    probe = root / ".tidewatch-write-probe"
    try:
        probe.write_text("", encoding="utf-8")
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()
    return True


def seed_user_root(bundled: Path | None = None, *, force: bool = False) -> list[str]:
    """把包内的默认资源复制到用户目录，返回实际复制出来的条目名。

    只在「exe 同级目录不可用」时才会被调用：程序装在只读位置时，
    使用者仍然需要一份能改的 `configs/` 与 `rules/`。
    已经存在的文件一律不覆盖 —— 覆盖等于把使用者改过的规则吃掉。
    """
    source = bundled or bundled_dir()
    if source is None or not _looks_like_resource_root(source):
        return []
    target = user_root()
    copied: list[str] = []
    for name in RESOURCE_DIRS:
        src = source / name
        if not src.exists():
            continue
        dst = target / name
        for item in src.rglob("*"):
            if not item.is_file():
                continue
            out = dst / item.relative_to(src)
            if out.exists() and not force:
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, out)
            copied.append(str(out))
    return copied


def resource_root() -> Path:
    """返回实际要用的资源根（含 `configs/` 与 `rules/` 的那一层）。

    都找不到时**不抛异常**，而是返回程序目录 ——
    这样调用方给出的报错会是一句「找不到配置文件: <某个具体路径>」，
    比在 import 阶段抛一个没有上下文的异常好排错得多。
    """
    for _label, candidate in search_locations():
        if _looks_like_resource_root(candidate):
            return candidate
    return app_dir()


def describe_paths() -> dict[str, str]:
    """给自检用的一组路径说明。每一项都是已解析的绝对路径。"""
    return {
        "运行形态": "打包 exe" if is_frozen() else "源码",
        "程序目录": str(app_dir()),
        "包内解压目录": str(bundled_dir()) if bundled_dir() else "（源码运行，无）",
        "资源根": str(resource_root()),
        "用户目录": str(user_root()),
    }
