"""配置层。

三级覆盖，优先级从高到低：
  1. 命令行 `--set a.b.c=1`
  2. 环境变量 `TW__a__b__c=1`（容器编排里不方便传带点的变量名，所以用双下划线）
  3. YAML 文件

把「所有会变的参数」都收进配置文件，是为了让对照实验变成「改配置 + 重跑」，
而不是「改代码 + 重跑」。这是这个项目能持续做实验的前提。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import yaml

__all__ = ["Config", "PROJECT_ROOT", "DEFAULT_CONFIG_PATH", "ENV_PREFIX", "env_overrides"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "default.yaml"

ENV_PREFIX = "TW__"


def env_overrides() -> list[str]:
    """从环境变量收集配置覆盖项，按变量名排序保证结果稳定。"""
    items: list[str] = []
    for key in sorted(os.environ):
        if not key.startswith(ENV_PREFIX):
            continue
        path = key[len(ENV_PREFIX) :].replace("__", ".").strip(".").lower()
        if path:
            items.append(f"{path}={os.environ[key]}")
    return items


def _coerce(value: str) -> Any:
    """把字符串按 YAML 语义还原成合适的类型。

    直接复用 yaml.safe_load 比自己写一堆 if 更可靠 —— 它能正确认出
    开头的 0x/0o、科学计数法、yes/no 等 YAML 特有写法。
    """
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError:
        return value


class Config:
    """点路径访问的配置对象，支持三级覆盖与指纹。"""

    def __init__(self, data: dict[str, Any] | None = None, overrides: Iterable[str] | None = None):
        self._data: dict[str, Any] = copy.deepcopy(data) if data else {}
        for item in list(overrides or []):
            if "=" not in item:
                raise ValueError(f"覆盖项必须是 key=value 形式，收到: {item!r}")
            key, value = item.split("=", 1)
            self.set(key.strip(), _coerce(value.strip()))

    # ---------------- 读取 ----------------

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, path: str) -> Any:
        sentinel = object()
        got = self.get(path, sentinel)
        if got is sentinel:
            raise KeyError(f"配置缺少必需项: {path}")
        return got

    def set(self, path: str, value: Any) -> None:
        parts = path.split(".")
        node = self._data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    def section(self, path: str) -> dict[str, Any]:
        got = self.get(path, {})
        return got if isinstance(got, dict) else {}

    def path(self, key: str, default: str = "") -> Path:
        """把配置里的相对路径解析成绝对路径（相对项目根）。"""
        raw = self.get(key, default) or default
        p = Path(str(raw))
        return p if p.is_absolute() else PROJECT_ROOT / p

    def signal_cfg(self, name: str) -> dict[str, Any]:
        """取某个信号的配置段，并补上通用默认值。

        `name` 形如 `text.template`，对应 YAML 里的 `signals: text: template:`。
        这里刻意不用扁平的含点键名 —— 配置查找用点做路径分隔符，两者混用会
        静默查不到、让权重悄悄回落成默认值，是个很难发现的坑。
        """
        raw = self.section(f"signals.{name}")
        raw.setdefault("enabled", True)
        raw.setdefault("weight", 1.0)
        return raw

    def iter_signals(self) -> Iterator[tuple[str, dict[str, Any]]]:
        """展开 signals 下的两层嵌套，产出 (完整信号名, 配置) 。"""
        for kind, group in sorted(self.section("signals").items()):
            if not isinstance(group, dict):
                continue
            for name, raw in sorted(group.items()):
                if isinstance(raw, dict):
                    yield f"{kind}.{name}", self.signal_cfg(f"{kind}.{name}")

    # ---------------- 指纹 ----------------

    def fingerprint(self) -> str:
        """配置指纹：参数一改就变，用于让实验结果能和参数对上。"""
        blob = json.dumps(self._data, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def with_overrides(self, *overrides: str) -> Config:
        """在一份已加载的配置上叠加覆盖项，返回**新**对象（原对象不变）。

        消融实验需要在同一份基线上派生好几个变体，改一个变体绝不能污染
        另一个 —— 否则后跑的组会带着前一组关掉的信号，结论全乱。
        """
        return Config(self._data, overrides)

    # ---------------- 加载 ----------------

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        overrides: Iterable[str] | None = None,
    ) -> Config:
        """加载配置。优先级：命令行 --set > 环境变量 > YAML。"""
        cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
        if not cfg_path.is_absolute():
            cfg_path = PROJECT_ROOT / cfg_path
        if not cfg_path.exists():
            raise FileNotFoundError(f"找不到配置文件: {cfg_path}")
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        merged = [*env_overrides(), *(overrides or [])]
        return cls(data, merged)

    def __repr__(self) -> str:
        return f"Config(fingerprint={self.fingerprint()}, signals={len(self.section('signals'))})"
