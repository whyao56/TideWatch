"""文本层基础设施：归一化、抗规避折叠、分词、以及一组纯统计算法。

这里所有函数都是无状态、可单测的，也刻意不依赖 numpy —— 别人可以把单个
函数拷进自己的项目直接用，这也是「可微调」的一种形态。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "normalize_text",
    "fold_for_match",
    "tokenize",
    "ngrams",
    "ngram_repeat_ratio",
    "shannon_entropy",
    "jaccard",
    "CommonSegment",
    "longest_common_segment",
    "longest_common_substring_ratio",
    "simhash",
    "hamming_distance",
    "hash_similarity",
]

# --------------------------------------------------------------------------
# 归一化
# --------------------------------------------------------------------------

_ZERO_WIDTH = dict.fromkeys(
    [ord(c) for c in "\u200b\u200c\u200d\u2060\ufeff\u00ad"], None
)

# 只把全角**字母数字**折成半角。刻意不含标点：
# 中文的全角逗号句号（，。：）本来就是正确写法，
# 用 NFKC 一把梭会把它们全转成半角，既难看，又会破坏后续按标点断句的逻辑。
#
# 注意键必须是 `ord()` 返回的**码点整数**，不能是字符本身：
# str.translate 是拿码点去查表的，传字符键的话查不到也不会报错，
# 整张表会静默空转 —— 这个坑踩过一次，所以下面专门留了一条测试锁住它。
_FULLWIDTH_ALNUM = {
    **{0xFF10 + i: chr(0x30 + i) for i in range(10)},
    **{0xFF21 + i: chr(0x41 + i) for i in range(26)},
    **{0xFF41 + i: chr(0x61 + i) for i in range(26)},
    ord("\u3000"): " ",
}

_CJK = re.compile(r"[\u4e00-\u9fff]")
_ASCII_WORD = re.compile(r"[a-z0-9]")
_WHITESPACE = re.compile(r"[ \t]+")


def normalize_text(text: str) -> str:
    """温和归一化：清零宽字符、全角字母数字折半角、压空白。

    这一步是「保真」的，用于统计特征计算。要防规避请用 fold_for_match。
    """
    if not text:
        return ""
    text = text.translate(_ZERO_WIDTH)
    text = text.translate(_FULLWIDTH_ALNUM)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fold_for_match(text: str, mapping: Mapping[str, str] | None = None) -> str:
    """激进的「抗规避折叠」，专供话术库匹配使用。

    水军规避关键词的手法非常朴素但有效：插符号（加·微·信）、用谐音
    （加薇信 / 威信）、上全角、夹 emoji、混形近字（微→薇/溦/威）。
    直接拿原文匹配话术库，召回率会低得没法看。

    做法是**逐字符白名单过滤 + 同义折叠**：只保留汉字和字母数字，
    其余一律丢弃，避免出现「加 微 信」因为空格而匹配不上「加微信」。
    丢弃后再套一遍形近字映射表。

    注意这是有损的：折叠后的文本只用于匹配，**绝不能**用于展示或统计，
    否则「加薇❤️信」会被当成「加微信」写进报告，误导使用者。
    """
    if not text:
        return ""
    mapping = mapping or {}
    out: list[str] = []
    for ch in text:
        mapped = mapping.get(ch)
        if mapped is None:
            lowered = ch.lower()
            mapped = mapping.get(lowered)
        else:
            lowered = ch.lower()

        if mapped is not None:
            out.append(mapped)
            continue
        if _CJK.match(ch):
            out.append(ch)
        elif _ASCII_WORD.match(lowered):
            out.append(lowered)
    return "".join(out)


# --------------------------------------------------------------------------
# 分词
# --------------------------------------------------------------------------

_jieba = None


def _get_jieba():
    """惰性加载 jieba。

    除了省启动时间，更重要的是：jieba 首次分词有三百毫秒左右的词典预热。
    放进函数里能让「没装 jieba」的环境自动走降级路径，而不是直接 import 报错。
    """
    global _jieba
    if _jieba is None:
        try:
            import jieba  # type: ignore

            jieba.setLogLevel(60)
            _jieba = jieba
        except ImportError:
            _jieba = False
    return _jieba or None


def tokenize(
    text: str,
    *,
    stopwords: Iterable[str] | None = None,
    drop_stopwords: bool = True,
) -> list[str]:
    """中文分词，jieba 不可用时降级为字符 bigram。

    降级路径不是摆设：它保证在没有 jieba 的环境（精简 CI、只用标准库的
    使用者）里依然能出结果，只是粒度更粗。
    """
    text = normalize_text(text)
    if not text:
        return []

    jieba = _get_jieba()
    if jieba is not None:
        tokens = [t.strip() for t in jieba.cut(text) if t.strip()]
    else:
        compact = re.sub(r"\s+", "", text)
        tokens = [compact[i : i + 2] for i in range(len(compact) - 1)] or [compact]

    if drop_stopwords and stopwords:
        stops = set(stopwords)
        tokens = [t for t in tokens if t not in stops]
    return tokens


# --------------------------------------------------------------------------
# 统计特征
# --------------------------------------------------------------------------


def ngrams(seq: Sequence[str], n: int) -> list[tuple[str, ...]]:
    if n <= 0 or len(seq) < n:
        return []
    return [tuple(seq[i : i + n]) for i in range(len(seq) - n + 1)]


def ngram_repeat_ratio(tokens: Sequence[str], n: int = 3) -> float:
    """重复 n-gram 的比例，作为「模板化程度」的直接代理指标。

    原理：正常人类表达里，三字组合的重复率极低；一段话反复复用同样的
    三字组合，通常意味着它是从模板里填出来的（换几个名词就再发一遍），
    或者由多段复制粘贴拼成。

    返回 1 - 唯一数/总数，范围 [0, 1)。词太少时返回 0（不表态）。
    """
    grams = ngrams(tokens, n)
    if len(grams) < 4:
        return 0.0
    unique = len(set(grams))
    return 1.0 - unique / len(grams)


def shannon_entropy(tokens: Sequence[str]) -> float:
    """按 token 分布计算的香农熵（bit）。

    这个值要**双向解读**，不能单向判定：
    - 异常低：内容高度重复（「好评好评好评」）
    - 异常高：插入大量互不相关的字符来绕开匹配
    所以信号层拿它和「重复率」配合使用，而不是单独下结论。
    """
    if not tokens:
        return 0.0
    counts = Counter(tokens)
    total = len(tokens)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """集合 Jaccard 相似度。"""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


@dataclass(slots=True)
class CommonSegment:
    """最长公共子串的位置信息，用于在报告里定位「抄的是哪一段」。"""

    length: int
    a_start: int
    b_start: int


def longest_common_segment(a: str, b: str) -> CommonSegment:
    """最长公共**子串**（连续），返回长度与两端起点。

    刻意用子串而不是子序列：照抄在文本上表现为**连续片段的雷同**；
    子序列会因为中文里共享常用字（的、了、是）而产生大量假阳性。

    用滚动数组把空间从 O(nm) 压到 O(min(n,m))。短文本评论场景下
    时间 O(nm) 完全够用，不值得为此上后缀自动机。
    """
    if not a or not b:
        return CommonSegment(0, 0, 0)
    if len(a) > len(b):
        a, b = b, a

    prev = [0] * (len(a) + 1)
    best = CommonSegment(0, 0, 0)
    for j in range(1, len(b) + 1):
        cur = [0] * (len(a) + 1)
        bj = b[j - 1]
        for i in range(1, len(a) + 1):
            if a[i - 1] == bj:
                cur[i] = prev[i - 1] + 1
                if cur[i] > best.length:
                    best = CommonSegment(cur[i], i - cur[i], j - cur[i])
        prev = cur
    return best


def longest_common_substring_ratio(a: str, b: str) -> float:
    """最长公共子串 / **较长串**长度。

    用较长者作分母是刻意的：它衡量的是「较长的那条里有多少比例是抄来的」。
    用较短者作分母会让短文本之间的误报率飙升 —— 两条三十来字的评论只要共用
    一句常见的十七字表达，比例就过半了，而那只说明中文表达有惯性，
    不说明它们在协同。真实场景里这种「共享一个常用短语」太常见了。

    对真正的照抄（两条几乎一样，或一条整段嵌进另一条）这个改动没有影响：
    那种情况下较长者本身也几乎全是公共段，比例照样接近 1。
    """
    if not a or not b:
        return 0.0
    seg = longest_common_segment(a, b)
    return seg.length / max(len(a), len(b))


def _hash64(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big")


def simhash(tokens: Sequence[str], bits: int = 64) -> int:
    """SimHash 指纹。

    原理：把每个 token 哈希成 bits 位，逐位投票 —— 该位为 1 则 +1、为 0 则 -1，
    最后按符号汇总成指纹。相近的文本只在少数位上产生分歧，于是汉明距离
    成了「内容相似度」的廉价近似，可以拿来给海量文本做去重与聚类。
    """
    if not tokens:
        return 0
    vector = [0] * bits
    for token in tokens:
        h = _hash64(token)
        for i in range(bits):
            vector[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if vector[i] > 0:
            out |= 1 << i
    return out


def hamming_distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def hash_similarity(a: int, b: int, bits: int = 64) -> float:
    """把汉明距离换算成 [0,1] 的相似度。"""
    if a == 0 and b == 0:
        return 0.0
    return 1.0 - hamming_distance(a, b) / bits
