"""文本层信号：最便宜、每条都能算，但也最容易被规避 —— 所以权重最低。

四个信号的分工刻意不重叠：
  template    抓「整段复用」（n-gram 级）
  repetition  抓「字符堆砌」（字符级）
  keyword     抓「说了什么」（话术库 + 正则）
  evasion     抓「刻意绕开」（折叠后才命中的差集）
"""

from __future__ import annotations

import re
from collections import Counter

from tidewatch.signals.base import ItemSignal, register
from tidewatch.text import ngram_repeat_ratio, ngrams, normalize_text, tokenize
from tidewatch.types import Dataset, Item, Level, SignalHit
from tidewatch.utils import clamp

__all__ = ["TemplateSignal", "RepetitionSignal", "KeywordSignal", "EvasionSignal"]

_WHITESPACE = re.compile(r"\s+")


def _max_run_length(text: str) -> int:
    """最长连续相同字符的长度。「哈哈哈哈哈」-> 6。"""
    if not text:
        return 0
    best = cur = 1
    for i in range(1, len(text)):
        if text[i] == text[i - 1]:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 1
    return best


@register
class TemplateSignal(ItemSignal):
    name = "text.template"
    description = "单条内部冗余：一句话里反复出现同样的 n-gram"
    principle = (
        "注意这和协同层的文本聚类是两回事：这里检测的是**一条发言内部**的冗余"
        "（把同一句话重复几遍凑长度、或者用复读来刷存在感），"
        "而「多条发言互相雷同」属于协同层的职责。"
        "正常表达里三字组合几乎不会自我重复，用 1 - 唯一n-gram数/n-gram总数 度量。"
    )

    def evaluate(self, item: Item, data: Dataset) -> list[SignalHit]:
        n = int(self.cfg.get("n", 3))
        min_tokens = int(self.cfg.get("min_tokens", 12))
        tokens = tokenize(item.text, stopwords=self.rules.stopwords)
        if len(tokens) < min_tokens:
            return []

        ratio = ngram_repeat_ratio(tokens, n)
        weak = float(self.cfg.get("weak", 0.22))
        strong = float(self.cfg.get("strong", 0.42))
        if ratio < weak:
            return []

        level = Level.STRONG if ratio >= strong else Level.WEAK
        intensity = clamp(ratio / max(strong, 1e-6), 0.3, 1.0)

        counter = Counter(ngrams(tokens, n))
        repeated = ["".join(g) for g, c in counter.most_common(3) if c > 1]
        return [
            self.make_hit(
                item.item_id,
                level,
                intensity,
                f"文本高度模板化（{n}-gram 重复率 {ratio:.0%}）",
                {
                    "repeat_ratio": round(ratio, 4),
                    "tokens": len(tokens),
                    "repeated_phrases": repeated,
                },
            )
        ]


@register
class RepetitionSignal(ItemSignal):
    name = "text.repetition"
    description = "刷屏复读：单字符占比过高或存在超长连续重复"
    principle = (
        "与模板化互补：模板化抓整段复用，这里抓字符级堆砌。"
        "「好评好评好评」这类内容在字符分布上极其集中，最高频字符占比会明显偏高；"
        "而「哈哈哈哈哈」「!!!!!!!!」则表现为超长连续重复。两个指标互相补充。"
    )

    def evaluate(self, item: Item, data: Dataset) -> list[SignalHit]:
        min_chars = int(self.cfg.get("min_chars", 10))
        compact = _WHITESPACE.sub("", normalize_text(item.text))
        if len(compact) < min_chars:
            return []

        char, count = Counter(compact).most_common(1)[0]
        ratio = count / len(compact)
        run = _max_run_length(compact)

        weak_ratio = float(self.cfg.get("char_ratio", 0.45))
        strong_ratio = float(self.cfg.get("strong_ratio", 0.6))
        run_limit = int(self.cfg.get("run_length", 6))

        if ratio >= strong_ratio or run >= run_limit * 2:
            level = Level.STRONG
        elif ratio >= weak_ratio or run >= run_limit:
            level = Level.WEAK
        else:
            return []

        intensity = clamp(max(ratio / max(strong_ratio, 1e-6), run / max(run_limit * 2, 1)), 0.3, 1.0)
        return [
            self.make_hit(
                item.item_id,
                level,
                intensity,
                f"疑似刷屏复读（最高频字符「{char}」占 {ratio:.0%}，最长连续重复 {run}）",
                {"char_ratio": round(ratio, 4), "top_char": char, "max_run": run},
            )
        ]


@register
class KeywordSignal(ItemSignal):
    name = "text.keyword"
    description = "话术库与正则规则命中"
    principle = (
        "用 AC 自动机一次扫描命中全部话术，复杂度与话术条数无关；"
        "再叠加正则捕捉手机号、外链这类枚举不完的结构化特征。"
        "强度取「命中项的最高权重」，并随命中的不同类目数小幅上浮 —— "
        "一条评论里同时出现导流、刷评、外链，比只出现一种更值得怀疑。"
    )

    def evaluate(self, item: Item, data: Dataset) -> list[SignalHit]:
        tags: dict[str, set[str]] = {}

        for match in self.rules.scan_phrases(item.text):
            tags.setdefault(match.tag, set()).add(match.pattern)
        for rule, sample in self.rules.scan_regex(item.text):
            tags.setdefault(rule.tag, set()).add(sample)

        if not tags:
            return []

        weights = [
            self.rules.group_weight(tag) if tag in {g.tag for g in self.rules.phrases}
            else self.rules.regex_weight(tag)
            for tag in tags
        ]
        peak = max(weights)
        # 类目越多，越不像偶然：单条里凑齐三种导流话术不是正常行为
        bonus = min(0.25, 0.08 * (len(tags) - 1))
        intensity = clamp(peak + bonus, 0.2, 1.0)

        strong_tags = set(self.cfg.get("strong_tags", []))
        level = Level.STRONG if (strong_tags & set(tags)) or intensity >= 0.9 else Level.WEAK

        return [
            self.make_hit(
                item.item_id,
                level,
                intensity,
                "命中话术/规则：" + "、".join(f"{t}({len(v)})" for t, v in tags.items()),
                {tag: sorted(v)[:5] for tag, v in tags.items()},
            )
        ]


@register
class EvasionSignal(ItemSignal):
    name = "text.evasion"
    description = "规避意图：靠抗规避折叠才捞出来的话术"
    principle = (
        "先按原文匹配一遍话术库，再按折叠后的文本匹配一遍，两者的差集就是"
        "「刻意规避」的证据：正常用户不会把「微信」写成「薇❤️信」或「加·微·信」。"
        "这是文本层里精度最高的信号 —— 误用这些变体几乎没有别的动机。"
    )

    def evaluate(self, item: Item, data: Dataset) -> list[SignalHit]:
        direct = {m.pattern for m in self.rules.scan_phrases(item.text)}
        folded = {m.pattern for m in self.rules.scan_phrases_folded(item.text)}
        extra = folded - direct

        min_extra = int(self.cfg.get("min_extra", 1))
        if len(extra) < min_extra:
            return []

        intensity = clamp(0.7 + 0.1 * (len(extra) - 1), 0.7, 1.0)
        return [
            self.make_hit(
                item.item_id,
                Level.STRONG,
                intensity,
                "存在刻意规避写法：" + "、".join(sorted(extra)[:5]),
                {"evaded": sorted(extra), "direct": sorted(direct)},
            )
        ]
