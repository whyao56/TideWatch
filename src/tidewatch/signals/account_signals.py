"""账号层信号：弱信号层。

这一层的定位必须说在最前面 —— **它永远不该单独下结论。**

「新注册 + 无头像 + 默认昵称」是水军的常见画像，但它同样是大量正常新用户的
画像。按这组特征单独筛人，误报率会高到没法用（这正是很多自建风控系统翻车
的地方）。所以这一层在设计上有三个刻意的约束：

  1. 权重压低（configs 里 0.5~0.8，是三层里最低的）
  2. 理由文案里明确带「需结合其他信号」，避免使用者误读
  3. 特征采用**叠加**而非**单项**触发 —— 单个特征只累加少量分值，
     凑齐多个才够得着嫌疑档位

它的作用是给文本层和协同层的证据做佐证，不是自己定案。
"""

from __future__ import annotations

from tidewatch.signals.base import ActorSignal, register
from tidewatch.text import jaccard, normalize_text
from tidewatch.types import Actor, Dataset, Item, Level, SignalHit
from tidewatch.utils import clamp, safe_div

__all__ = ["AccountAgeSignal", "OriginalitySignal", "ProfileSignal"]

# 自身发言两两比较是 O(n^2) 的，样本上限做个保护。
# 真要处理发帖上万的账号，应该换 SimHash 分桶找近邻，而不是继续加大这个数。
_MAX_SAMPLE = 60
_SHINGLE = 5


def _shingles(text: str, k: int = _SHINGLE) -> set[str]:
    s = normalize_text(text)
    if len(s) < k:
        return {s} if s else set()
    return {s[i : i + k] for i in range(len(s) - k + 1)}


@register
class AccountAgeSignal(ActorSignal):
    name = "account.age"
    description = "账号年龄过短"
    principle = (
        "水军账号多为一次性消耗品：注册后短期集中使用，用完即弃。"
        "但这是弱证据 —— 每天都有大量正常新用户注册，"
        "所以它只做佐证，绝不单独定案。"
    )

    def evaluate(self, actor: Actor, items: list[Item], data: Dataset) -> list[SignalHit]:
        fresh = float(self.cfg.get("fresh_days", 30))
        very_fresh = float(self.cfg.get("very_fresh_days", 7))

        # 关键：用**数据自身的时间上界**当作「现在」，而不是 datetime.now()。
        # 用当前时刻算年龄，分析历史数据时结论会完全反过来 —— 一个 2020 年
        # 注册的水军号，按「从那时到今天」会算出注册了六年，判定自然失效。
        _, latest = data.time_span
        age = actor.age_days(latest)
        if age is None or age >= fresh:
            return []

        level = Level.STRONG if age <= very_fresh else Level.WEAK
        # 越新越可疑：7 天以内给满强度，接近 30 天降到下限
        intensity = clamp(1.0 - age / fresh, 0.3, 1.0)
        return [
            self.make_hit(
                actor.actor_id,
                level,
                intensity,
                f"账号注册仅 {age:.0f} 天（需结合其他信号）",
                {"age_days": round(age, 1)},
            )
        ]


@register
class OriginalitySignal(ActorSignal):
    name = "account.originality"
    description = "账号自身发言之间高度重复"
    principle = (
        "正常用户的发言是「一个人在不同场合说的话」，彼此差异大；"
        "水军账号的内容常常是同一份文案反复发，或从同一个素材库批量取用。"
        "用账号内部两两相似度超阈值的比例来度量。"
        "注意是**自比**：跨账号的重复属于协同层，不在这里做，避免两层重复计分。"
    )

    def evaluate(self, actor: Actor, items: list[Item], data: Dataset) -> list[SignalHit]:
        min_posts = int(self.cfg.get("min_posts", 5))
        if len(items) < min_posts:
            return []
        sample = items[:_MAX_SAMPLE]
        shingles = [_shingles(i.text) for i in sample]

        pairs = 0
        duplicates = 0
        for i in range(len(shingles)):
            for j in range(i + 1, len(shingles)):
                if not shingles[i] or not shingles[j]:
                    continue
                pairs += 1
                if jaccard(shingles[i], shingles[j]) >= 0.6:
                    duplicates += 1

        ratio = safe_div(duplicates, pairs)
        threshold = float(self.cfg.get("copy_ratio", 0.6))
        if ratio < threshold or pairs == 0:
            return []

        return [
            self.make_hit(
                actor.actor_id,
                Level.STRONG if ratio >= threshold * 1.3 else Level.WEAK,
                clamp(ratio / max(threshold * 1.3, 1e-6), 0.4, 1.0),
                f"该账号 {ratio:.0%} 的发言与其他发言高度重复",
                {"copy_ratio": round(ratio, 4), "pairs": pairs, "sampled": len(sample)},
            )
        ]


@register
class ProfileSignal(ActorSignal):
    name = "account.profile"
    description = "资料异常：默认昵称 / 无头像 / 关注粉丝比失衡"
    principle = (
        "把几个弱特征**叠加**成一个复合分。单独看每个都不足以说明问题"
        "（默认昵称可能只是懒，无头像也可能是隐私偏好），但三个同时出现时，"
        "这个账号的成本结构就非常像「批量购入的一次性号」。"
        "叠加而非单项触发，是这一层不产生大量误报的关键。"
    )

    def evaluate(self, actor: Actor, items: list[Item], data: Dataset) -> list[SignalHit]:
        ratio_threshold = float(self.cfg.get("follower_ratio", 5.0))
        score = 0.0
        reasons: list[str] = []

        if actor.is_default_name:
            score += 0.3
            reasons.append("使用默认昵称")
        if not actor.has_avatar:
            score += 0.2
            reasons.append("无头像")
        if actor.follower_ratio >= ratio_threshold:
            score += 0.3
            reasons.append(f"关注/粉丝比 {actor.follower_ratio:.1f}")
        if actor.rename_count is not None and actor.rename_count >= 5:
            score += 0.2
            reasons.append(f"改名 {actor.rename_count} 次")

        if score < 0.4:
            return []

        return [
            self.make_hit(
                actor.actor_id,
                Level.STRONG if score >= 0.6 else Level.WEAK,
                clamp(score, 0.3, 1.0),
                "资料特征叠加异常：" + "、".join(reasons) + "（需结合其他信号）",
                {"profile_score": round(score, 2), "features": reasons},
            )
        ]
