"""外置规则库 —— 别人微调这个项目**最主要**的入口。

设计意图很直接：想适配一个新领域（游戏、电商、政务），不需要读懂任何一行
Python，改 YAML 就行。所以这里刻意把三样东西都做成了纯数据：

  - phrases.yaml   话术库（用 AC 自动机匹配，支持 tag 分组和权重）
  - evasion.yaml   抗规避折叠表（谐音 / 形近字 / 全角 / 符号）
  - patterns.yaml  正则规则（手机号、QQ、外链这类结构化特征）

RuleBook 本身是只读的，加载后不修改 —— 这样同一份规则可以安全地被多个
信号共享，也不用担心某个信号改坏了别人的输入。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tidewatch.matching import AhoCorasick, Match
from tidewatch.text import fold_for_match, normalize_text

__all__ = ["PhraseGroup", "RegexRule", "RuleBook"]


@dataclass(slots=True)
class PhraseGroup:
    """一组同类话术。tag 用于分类统计，weight 用于调节这一组的强度。"""

    tag: str
    weight: float
    phrases: tuple[str, ...]


@dataclass(slots=True)
class RegexRule:
    tag: str
    weight: float
    desc: str
    pattern: re.Pattern[str]


@dataclass(slots=True)
class RuleBook:
    """加载后的规则全集。"""

    phrases: tuple[PhraseGroup, ...] = ()
    fold_map: dict[str, str] = field(default_factory=dict)
    regexes: tuple[RegexRule, ...] = ()
    stopwords: frozenset[str] = frozenset()
    _matcher: AhoCorasick | None = None

    # ---------------- 加载 ----------------

    @classmethod
    def load(cls, directory: str | Path) -> RuleBook:
        root = Path(directory)
        return cls(
            phrases=cls._load_phrases(root / "phrases.yaml"),
            fold_map=cls._load_fold(root / "evasion.yaml"),
            regexes=cls._load_regexes(root / "patterns.yaml"),
            stopwords=cls._load_stopwords(root / "stopwords.txt"),
        )

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    @classmethod
    def _load_phrases(cls, path: Path) -> tuple[PhraseGroup, ...]:
        data = cls._read_yaml(path)
        groups: list[PhraseGroup] = []
        for raw in data.get("groups", []):
            phrases = tuple(str(p).strip() for p in raw.get("phrases", []) if str(p).strip())
            if not phrases:
                continue
            groups.append(
                PhraseGroup(
                    tag=str(raw.get("tag", "未分类")),
                    weight=float(raw.get("weight", 1.0)),
                    phrases=phrases,
                )
            )
        return tuple(groups)

    @classmethod
    def _load_fold(cls, path: Path) -> dict[str, str]:
        data = cls._read_yaml(path)
        raw = data.get("fold", {}) or {}
        return {str(k): str(v) for k, v in raw.items() if str(k)}

    @classmethod
    def _load_regexes(cls, path: Path) -> tuple[RegexRule, ...]:
        data = cls._read_yaml(path)
        out: list[RegexRule] = []
        for raw in data.get("rules", []):
            expr = raw.get("pattern")
            if not expr:
                continue
            try:
                compiled = re.compile(str(expr))
            except re.error:
                # 单条规则写错不该拖垮整个规则库 —— 使用者自己写的 YAML 里
                # 一个转义失误就导致程序起不来，是最劝退的体验。
                continue
            out.append(
                RegexRule(
                    tag=str(raw.get("tag", "未分类")),
                    weight=float(raw.get("weight", 1.0)),
                    desc=str(raw.get("desc", "")),
                    pattern=compiled,
                )
            )
        return tuple(out)

    @staticmethod
    def _load_stopwords(path: Path) -> frozenset[str]:
        if not path.exists():
            return frozenset()
        words = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                words.append(line)
        return frozenset(words)

    # ---------------- 查询 ----------------

    def matcher(self) -> AhoCorasick:
        """构建（并缓存）话术自动机。"""
        if self._matcher is None:
            ac = AhoCorasick(ignore_case=True)
            for group in self.phrases:
                ac.extend(group.phrases, tag=group.tag)
            self._matcher = ac.build()
        return self._matcher

    def group_weight(self, tag: str) -> float:
        for group in self.phrases:
            if group.tag == tag:
                return group.weight
        return 1.0

    def regex_weight(self, tag: str) -> float:
        for rule in self.regexes:
            if rule.tag == tag:
                return rule.weight
        return 1.0

    def scan_phrases(self, text: str) -> list[Match]:
        """在**原文**上匹配话术库。"""
        if not text:
            return []
        return self.matcher().find(text)

    def scan_phrases_folded(self, text: str) -> list[Match]:
        """在**折叠后**的文本上匹配，用来发现带规避意图的写法。

        和 scan_phrases 的差集就是「这条是靠折叠才捞出来的」，
        这个差集本身就是一条有效信号：正常用户不会刻意把「微信」写成「薇❤️信」。
        """
        if not text:
            return []
        folded = self.fold(text)
        return self.matcher().find(folded) if folded else []

    def fold(self, text: str) -> str:
        return fold_for_match(normalize_text(text), self.fold_map)

    def scan_regex(self, text: str) -> list[tuple[RegexRule, str]]:
        if not text:
            return []
        hits: list[tuple[RegexRule, str]] = []
        for rule in self.regexes:
            found = rule.pattern.findall(text)
            if found:
                first = found[0]
                sample = first if isinstance(first, str) else "".join(first)
                hits.append((rule, sample))
        return hits

    def stats(self) -> dict[str, Any]:
        return {
            "phrase_groups": len(self.phrases),
            "phrases": sum(len(g.phrases) for g in self.phrases),
            "fold_entries": len(self.fold_map),
            "regex_rules": len(self.regexes),
            "stopwords": len(self.stopwords),
        }

    @classmethod
    def empty(cls) -> RuleBook:
        """空规则库。用于测试，以及「没有规则文件也要能启动」这种场景。"""
        return cls()
