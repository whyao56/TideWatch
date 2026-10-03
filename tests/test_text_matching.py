"""文本层与匹配算法的测试。

这一组测试里最重要的是几条**回归测试** —— 它们对应的是「能跑但结果是错的」
那类问题，不写测试就会在后续改动里悄悄复发。
"""

from __future__ import annotations

import pytest

from tidewatch.matching import AhoCorasick
from tidewatch.text import (
    fold_for_match,
    hamming_distance,
    longest_common_segment,
    longest_common_substring_ratio,
    ngram_repeat_ratio,
    normalize_text,
    shannon_entropy,
    simhash,
    tokenize,
)
from tidewatch.utils import parse_time

# --------------------------------------------------------------------------
# 归一化：最重要的回归测试
# --------------------------------------------------------------------------


def test_normalize_keeps_chinese_punctuation() -> None:
    """回归：不能用 NFKC 归一化。

    NFKC 会把中文全角标点（，。：）折成半角，中文正文会被改成
    「逗号成英文逗号」的样子 —— 既难看，又会破坏后续按标点断句的逻辑。
    所以只允许折全角字母数字。
    """
    text = "这是一句话，第二个分句：结束。"
    assert normalize_text(text) == text


def test_normalize_folds_fullwidth_alnum() -> None:
    assert normalize_text("ＡＢＣ１２３") == "ABC123"


def test_normalize_strips_zero_width() -> None:
    assert normalize_text("正\u200b常\ufeff文本") == "正常文本"


# --------------------------------------------------------------------------
# 抗规避折叠
# --------------------------------------------------------------------------


def test_fold_restores_homophone() -> None:
    mapping = {"薇": "微", "溦": "微"}
    assert fold_for_match("加薇信", mapping) == "加微信"
    assert fold_for_match("加溦信", mapping) == "加微信"


def test_fold_drops_separators_and_emoji() -> None:
    """插符号和夹 emoji 是水军最常用的规避手法，必须被折叠掉。"""
    assert fold_for_match("加 微·信", {}) == "加微信"
    assert fold_for_match("加微❤️信", {}) == "加微信"
    assert fold_for_match("加-微-信", {}) == "加微信"


def test_fold_keeps_ascii_lowercased() -> None:
    assert fold_for_match("加V信", {}) == "加v信"
    assert fold_for_match("QQ号", {}) == "qq号"


# --------------------------------------------------------------------------
# AC 自动机
# --------------------------------------------------------------------------


def test_ac_finds_all_patterns() -> None:
    ac = AhoCorasick().extend(["加微信", "私聊"], tag="导流").build()
    found = {m.pattern for m in ac.find("有需要加微信详聊或者私聊我")}
    assert found == {"加微信", "私聊"}


def test_ac_reports_position_and_tag() -> None:
    ac = AhoCorasick().add("加微信", tag="导流").build()
    match = ac.find("请加微信")[0]
    assert (match.start, match.end) == (1, 4)
    assert match.tag == "导流"


def test_ac_handles_overlapping_suffixes() -> None:
    """fail 链合并的回归测试：短模式是长模式的后缀时必须也能命中。"""
    ac = AhoCorasick().extend(["abc", "bc", "c"]).build()
    found = {m.pattern for m in ac.find("abc")}
    assert found == {"abc", "bc", "c"}


def test_ac_is_case_insensitive_by_default() -> None:
    ac = AhoCorasick().add("QQ").build()
    assert ac.matched_patterns("加qq") == {"qq"}


def test_ac_empty_text() -> None:
    ac = AhoCorasick().add("x").build()
    assert ac.find("") == []


# --------------------------------------------------------------------------
# 统计特征
# --------------------------------------------------------------------------


def test_ngram_repeat_ratio_high_for_repeats() -> None:
    repeated = tokenize("好评好评好评好评好评好评好评好评")
    normal = tokenize("这个方案在真实场景里的表现还需要更多验证才能下结论")
    assert ngram_repeat_ratio(repeated) > ngram_repeat_ratio(normal)


def test_ngram_repeat_ratio_abstains_on_short_input() -> None:
    """词太少时不表态 —— 短文本的重复率天然偏高，据此报警会大量误报。"""
    assert ngram_repeat_ratio(tokenize("好评好评")) == 0.0


def test_entropy_lower_for_repeats() -> None:
    assert shannon_entropy(tokenize("好 好 好 好")) < shannon_entropy(
        tokenize("这个方法真的很有参考价值")
    )


# --------------------------------------------------------------------------
# 相似度
# --------------------------------------------------------------------------


def test_lcs_identical() -> None:
    assert longest_common_substring_ratio("完全一样的文本", "完全一样的文本") == 1.0


def test_lcs_uses_longer_as_denominator() -> None:
    """回归：分母必须用较长串。

    用较短串作分母时，两条三十来字的评论只要共用一句常见的十七字表达，
    比例就过半了，而那只说明中文表达有惯性，不说明它们在协同。
    """
    short = "不过在生产环境里可能还要补一些兜底"
    long = short + "，另外还要考虑权限和并发的问题，这些都得提前想清楚才行"
    ratio = longest_common_substring_ratio(short, long)
    assert ratio == pytest.approx(len(short) / len(long))
    assert ratio < 0.5


def test_lcs_returns_location() -> None:
    seg = longest_common_segment("前缀AB中间CD后缀", "xxAByy")
    assert seg.length == 2
    assert "前缀AB中间CD后缀"[seg.a_start : seg.a_start + seg.length] == "AB"


def test_simhash_distance() -> None:
    a = simhash(tokenize("这个方法真的很好用强烈推荐大家试试"))
    b = simhash(tokenize("这个方法真的很好用强烈推荐大家试一下"))
    c = simhash(tokenize("完全无关的另外一段内容在讲别的事情"))
    assert hamming_distance(a, b) < hamming_distance(a, c)


# --------------------------------------------------------------------------
# 时间解析
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected_year",
    [
        (1693526400, 2023),
        (1693526400000, 2023),  # 毫秒级：按秒解释会得到公元五万年
        ("2023-09-01T00:00:00", 2023),
        ("2023-09-01 00:00:00", 2023),
        ("2023-09-01", 2023),
    ],
)
def test_parse_time_formats(value, expected_year: int) -> None:
    parsed = parse_time(value)
    assert parsed is not None
    assert parsed.year == expected_year


def test_parse_time_rejects_garbage() -> None:
    assert parse_time("不是时间") is None
    assert parse_time(None) is None
    assert parse_time("") is None
