"""生成带已知协同模式的合成样例数据。

为什么需要它：真实水军标注极难获取（这是这个领域的公开难题，论文基本都得
自己标）。合成数据的用途**不是**用来吹指标，而是**验证算法原理是否正确** ——
我明确知道哪几条是水军、它们按什么模式协同，那么如果引擎没挖出来，就是算法
有问题，而不是标注有问题。这个「可证伪」的性质，是合成数据唯一的价值。

它不能证明的事：不能证明引擎在真实数据上的效果。真实数据上的表现必须用
公开数据集或自己的标注来验证。这一点在 README 的已知限制里写明了。

注入的模式（刻意覆盖三层信号）：
  1. 模板组  —— 8 个账号发同一文案的轻微变体      -> 文本层 + 协同文本聚类
  2. 爆发组  —— 15 个账号在 3 分钟内集中发帖       -> 协同时间爆发
  3. 团伙组  —— 6 个账号反复出现在同样的 4 个话题下 -> 协同共现图
  4. 导流组  —— 少量账号发带规避写法的评论          -> 文本层（抗规避折叠）
  5. 正常用户 —— 内容各异、时间分散、资料正常       -> 用于衡量误报
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tidewatch.io import write_jsonl  # noqa: E402

BASE_TIME = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
SEED = 20261003

# ---------------------------------------------------------------- 素材

# 正常评论用「开头 + 中段 + 结尾」三段组合生成，保证彼此各不相同。
#
# 这不是为了好看：如果正常评论只有二十来种模板，文本相似度聚类会把它们
# 误判成水军 —— 而那是数据集的缺陷，不是算法的问题。真实社区里正常发言的
# 多样性远高于此，合成数据必须体现这一点，否则验证出的结论是错的。
_OPENERS = [
    "刚看完这期内容",
    "跟着做了一遍之后",
    "这个方案我们组上个月评估过",
    "看到一半的时候有点疑惑",
    "之前做过类似的东西",
    "说实话一开始没太看懂",
    "结合我们自己项目的经验",
    "对比了几种实现方式以后",
    "把代码 clone 下来跑了一遍",
    "站在维护者的角度想",
    "从性能角度重新看了一遍",
    "和文档里写的对了一下",
    "在测试环境里验证了一下",
    "把这段逻辑单独抽出来看了下",
    "跟团队讨论之后",
    "翻了下相关的 issue",
    "用真实数据跑过一轮",
    "从零开始跟着实现了一遍",
    "对照着源码看了一会儿",
    "把这个思路和之前的方案比了比",
]

_MIDDLES = [
    "整体思路是清楚的",
    "中间那部分的推导有点跳",
    "最大的收获是知道该在哪一步做取舍",
    "发现边界情况的处理还有讨论空间",
    "感觉对新手来说门槛稍微高了点",
    "这个抽象层次设计得挺合理",
    "不过在生产环境里可能还要补一些兜底",
    "最关键的还是数据规模上去以后的表现",
    "有几处细节和我想的不太一样",
    "拆解方式比我之前见过的都更清晰",
    "兼容性方面可能需要额外注意",
    "错误处理写得比较到位",
    "文档如果配个完整例子会更好懂",
    "复杂度控制得不错",
    "不过扩展性上我觉得还有提升空间",
    "这个权衡讲得挺明白的",
    "实际落地时还要考虑团队的历史包袱",
    "接口设计得比较克制",
    "有没有考虑过并发场景下的问题",
    "和业界常见做法基本是一致的",
]

_CLOSERS = [
    "回头我试试看能不能用在我们那边",
    "已经收藏了慢慢消化",
    "期待后续能有更深入的展开",
    "感谢分享这个思路",
    "我再去补一下相关的背景知识",
    "有类似经验的朋友可以一起讨论下",
    "等我有实际结果了再来反馈",
    "打算这周抽时间动手验证一下",
    "先记下来备用",
    "希望能看到更多这一类的拆解",
]

TEMPLATE_VARIANTS = [
    "这个方法真的太好用了，强烈推荐大家试一下，绝对不会后悔",
    "这个方法真的太好用了，强烈推荐大家试一下，绝对不后悔",
    "这个方法真的太好用了，强烈推荐大家试一下，绝对不会后悔的",
    "这个方法真的太好用了，强烈推荐大家试试，绝对不会后悔",
    "这个方法真的太好用了，强烈推荐大家试一下，真心不后悔",
    "这个方法真的太好用了，强烈推荐大家试一下，绝对不会后",
    "这个方法真的好用，强烈推荐大家试一下，绝对不会后悔",
    "这个方法真的太好用了，强烈推荐大家试一下，绝对不后",
]

BURST_TEXTS = [
    "支持！说得很对",
    "说得太对了，支持",
    "支持一下，说到心坎里了",
    "顶！说得很对",
    "说得好，支持",
    "支持，说得对",
    "这个必须支持",
    "说得很在理",
    "赞同，说到点子上了",
    "支持楼主",
    "说得没错",
    "完全同意这个说法",
    "顶上去让更多人看到",
    "支持，希望更多人看到",
    "说得有道理，支持一下",
]

# 复读组：同一个账号把同一句话反复发（单账号行为，不是跨账号协同）。
# 刻意让每个账号发的内容互不相同 —— 这样只有账号层的原创度信号能抓到它们，
# 协同层的文本聚类抓不到。一个模式对应一个信号，验证结论才干净。
REPEAT_LINES = [
    "这个教程真的写得太好了建议大家一定都要认真看完",
    "强烈推荐这个做法简单又高效大家赶紧用起来吧",
    "这个方法我用了很久一直很稳定推荐给大家使用",
]

# 刷屏组：单条发言内部就存在大量重复（凑字数或复读）。
# 用来验证文本层的内部冗余检测 —— 和「多条之间雷同」是两回事。
FLOOD_TEXTS = [
    "好评好评好评好评好评好评好评好评好评好评",
    "这个方法好用这个方法好用这个方法好用这个方法好用",
    "哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈哈",
]

SHILL_VARIANTS = [
    "有需要的加薇❤️信详聊，价格可以谈",
    "需要的话加薇 信，详情私聊我",
    "想了解加溦信，我拉你进群",
    "有需要的加➕薇信 详聊",
    "加薇芯详聊，一手货源",
]


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _normal_actor(rng: random.Random, index: int) -> dict:
    created = BASE_TIME - timedelta(days=rng.randint(200, 2000))
    return {
        "actor_id": f"u-{index:04d}",
        "actor_name": f"用户{index}号",
        "actor_created_at": _iso(created),
        "actor_followers": rng.randint(5, 800),
        "actor_following": rng.randint(10, 400),
        "actor_posts": rng.randint(20, 900),
        "actor_has_avatar": True,
        "actor_default_name": False,
    }


def _fake_actor(rng: random.Random, group: str, index: int) -> dict:
    """水军账号画像：新注册、无头像、默认昵称、关注一堆但没人关注。"""
    created = BASE_TIME - timedelta(days=rng.randint(1, 12))
    return {
        "actor_id": f"fake-{group}-{index:02d}",
        "actor_name": f"用户{rng.randint(1000000, 9999999)}",
        "actor_created_at": _iso(created),
        "actor_followers": 0,
        "actor_following": rng.randint(200, 900),
        "actor_posts": rng.randint(1, 6),
        "actor_has_avatar": False,
        "actor_default_name": True,
    }


def generate() -> list[dict]:
    rng = random.Random(SEED)
    records: list[dict] = []
    counter = 0

    def emit(text, actor, created, topic, group, positive):
        nonlocal counter
        counter += 1
        records.append(
            {
                "item_id": f"c-{counter:05d}",
                "text": text,
                "topic": topic,
                "created_at": _iso(created),
                "is_synthetic_positive": positive,
                "group": group,
                **actor,
            }
        )

    # ---------------- 1) 正常讨论：120 条，跨 3 小时，分散在不同话题 ----------------
    # 关键：保证任意两条正常评论**不共享「开头+中段」这个组合**。
    # 否则它们会共享一段二十来字的连续文本，被最长公共子串判定为「照抄」——
    # 那是生成器的锅，不是算法的锅。真实社区的措辞不会这样系统性重合。
    pairs = [(o, m) for o in _OPENERS for m in _MIDDLES]
    rng.shuffle(pairs)
    pool = [f"{o}，{m}，{rng.choice(_CLOSERS)}" for o, m in pairs[:160]]
    for i in range(120):
        actor = _normal_actor(rng, i)
        topic = f"video-{1000 + rng.randint(0, 2)}"
        created = BASE_TIME + timedelta(seconds=rng.randint(0, 10800))
        emit(pool[i], actor, created, topic, "normal", False)

    # ---------------- 2) 模板组：8 个账号，同一话题，2 小时内发近似文案 ----------------
    # 时间刻意拉得很开（间隔 900 秒），这样它**只会被文本聚类抓到，不会被时间爆发
    # 抓到** —— 一个模式对应一个信号，验证出来的结论才干净。
    topic = "video-1001"
    for i, text in enumerate(TEMPLATE_VARIANTS):
        actor = _fake_actor(rng, "tpl", i)
        created = BASE_TIME + timedelta(seconds=3600 + i * 900)
        emit(text, actor, created, topic, "template", True)

    # ---------------- 3) 爆发组：15 个账号，3 分钟内集中投放 ----------------
    topic = "video-1002"
    for i in range(15):
        actor = _fake_actor(rng, "burst", i)
        created = BASE_TIME + timedelta(seconds=7200 + i * 11)
        emit(rng.choice(BURST_TEXTS), actor, created, topic, "burst", True)

    # ---------------- 4) 团伙组：6 个账号在同样 4 个话题下反复出现 ----------------
    members = [_fake_actor(rng, "gang", i) for i in range(6)]
    topics = ["video-1003", "video-1004", "video-1005", "video-1006"]
    for t_index, t_name in enumerate(topics):
        for m_index, actor in enumerate(members):
            created = BASE_TIME + timedelta(seconds=4000 + t_index * 900 + m_index * 40)
            # 团伙的每条文案也各不相同，这样它们只会被「共现图」抓到，
            # 不会顺带被文本聚类抓到 —— 两种模式分开验证，结论才干净。
            emit(pool[120 + t_index * len(members) + m_index], actor, created, t_name, "gang", True)

    # ---------------- 5) 导流组：带规避写法的评论 ----------------
    topic = "video-1007"
    for i, text in enumerate(SHILL_VARIANTS):
        actor = _fake_actor(rng, "shill", i)
        created = BASE_TIME + timedelta(seconds=9000 + i * 300)
        emit(text, actor, created, topic, "shill", True)

    # ---------------- 6) 复读组：同账号反复发同一句（账号层） ----------------
    topic = "video-1008"
    for i, line in enumerate(REPEAT_LINES):
        actor = _fake_actor(rng, "repeat", i)
        for k in range(6):
            created = BASE_TIME + timedelta(seconds=10000 + i * 600 + k * 90)
            emit(line, actor, created, topic, "repeat", True)

    # ---------------- 7) 刷屏组：单条内部高度重复（文本层） ----------------
    topic = "video-1009"
    for i, text in enumerate(FLOOD_TEXTS):
        actor = _fake_actor(rng, "flood", i)
        created = BASE_TIME + timedelta(seconds=11000 + i * 120)
        emit(text, actor, created, topic, "flood", True)

    return records


def main() -> None:
    records = generate()
    positives = sum(1 for r in records if r["is_synthetic_positive"])
    labelled = ROOT / "data" / "eval" / "synthetic_labeled.jsonl"
    sample = ROOT / "data" / "sample" / "sample.jsonl"

    write_jsonl(labelled, records)

    # 样例文件去掉标注字段，只留引擎需要的部分 —— 它是给别人看的「格式示例」，
    # 里面混着评测标签会让人误以为引擎依赖这个字段。
    clean = [{k: v for k, v in r.items() if not k.startswith("is_") and k != "group"} for r in records]
    write_jsonl(sample, clean)

    groups: dict[str, int] = {}
    for record in records:
        groups[record["group"]] = groups.get(record["group"], 0) + 1

    print(f"生成 {len(records)} 条（其中已知水军 {positives} 条）")
    print("分组：", "、".join(f"{k}={v}" for k, v in sorted(groups.items())))
    print(f"写入 {labelled.relative_to(ROOT)}")
    print(f"写入 {sample.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
