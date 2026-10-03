"""协同层信号 —— 这是整个项目的核心，也是唯一真正难做的一层。

为什么核心：对手可以伪造身份（新号、头像、昵称全都能刷），但很难伪造
「各自独立」。一群人要一起行动，就必然在时间、文本、关系上留下统计痕迹。

三个信号从三个不同维度捕捉「整齐」：
  burst      时间维度：同一话题在短时间内被集中轰炸
  cluster    文本维度：多条发言互为模板（整段复用或局部照抄）
  co_occur 关系维度：同一批账号反复出现在同一批话题里

它们产出的 target 是**团伙**（GROUP）而不是单条或账号 —— 这正是这一层
比前两层更有价值的地方：它指出的是「这群人是一伙的」，而不只是
「这条评论看起来怪」。
"""

from __future__ import annotations

from collections import Counter, defaultdict

from tidewatch.signals.base import CoordinationSignal, register
from tidewatch.text import hash_similarity, longest_common_substring_ratio, simhash, tokenize
from tidewatch.types import Dataset, Level, SignalHit, ensure_aware
from tidewatch.utils import clamp, safe_div

__all__ = ["BurstSignal", "TextClusterSignal", "CoOccurrenceSignal"]

# 单个话题下的两两比较是 O(n^2)。这里给出上界，避免一个热门视频的几万条
# 评论把整轮分析拖死。真要处理这个量级，应该上 SimHash 分桶 + LSH 索引。
_MAX_PER_TOPIC = 400


@register
class BurstSignal(CoordinationSignal):
    name = "coordination.burst"
    description = "时间爆发：同一话题在短时间内被集中投放"
    principle = (
        "「爆发」本质上是个**相对**判断 —— 脱离了正常速率，「快」就没有意义。"
        "所以先用滑动窗口找出话题里最密集的一段，把它的到达速率和**整个数据集**的"
        "背景速率相除，比值超过阈值才算爆发。\n"
        "背景速率刻意取自全数据集，而不是话题自己：用话题自身的平均密度当背景会"
        "自我抵消 —— 一个整体就是投放的话题，它的平均密度早就被投放本身拉高了，"
        "于是永远检不出自己。全数据集里通常含有大量自然讨论，是更稳的参照。\n"
        "代价是这份数据本身必须含有可比的正常活动：如果整批数据就是一段紧挨着的"
        "爆发（没有任何平缓的背景），信号会给出接近 1 的比值而**弃权** —— "
        "这是诚实的：没有背景，就不该硬下结论。\n"
        "选滑动窗口而不是固定分桶，是为了避免爆发恰好跨在桶边界上被拆成两半。"
    )

    def run(self, data: Dataset) -> list[SignalHit]:
        window = float(self.cfg.get("window_seconds", 300))
        min_items = int(self.cfg.get("min_items", 8))
        min_actors = int(self.cfg.get("min_actors", 3))
        ratio_threshold = float(self.cfg.get("density_ratio", 2.5))

        # 背景速率：全数据集（所有话题）的总体到达速率，条/秒。
        timed_all = sorted(
            (i for i in data.items if i.created_at is not None),
            key=lambda x: ensure_aware(x.created_at),
        )
        if len(timed_all) < min_items:
            return []
        bg_span = (
            ensure_aware(timed_all[-1].created_at) - ensure_aware(timed_all[0].created_at)
        ).total_seconds()
        # 时间全都相同（跨度 0）时取 1 秒作下界，避免除以零。
        background = len(timed_all) / max(bg_span, 1.0)

        out: list[SignalHit] = []
        for topic, items in data.by_topic().items():
            timed = sorted(
                (i for i in items if i.created_at is not None),
                key=lambda x: ensure_aware(x.created_at),
            )
            if len(timed) < min_items:
                continue

            best_count, best_left, best_right = 0, 0, 0
            left = 0
            for right in range(len(timed)):
                rt = ensure_aware(timed[right].created_at)
                while (rt - ensure_aware(timed[left].created_at)).total_seconds() > window:
                    left += 1
                count = right - left + 1
                if count > best_count:
                    best_count, best_left, best_right = count, left, right

            if best_count < min_items:
                continue

            cluster = timed[best_left : best_right + 1]
            t0 = ensure_aware(cluster[0].created_at)
            t1 = ensure_aware(cluster[-1].created_at)
            # 用这一段的**真实跨度**算速率，而不是名义窗口长度：
            # 8 条挤在 14 秒里，说它是「8 条 / 300 秒」会把爆发稀释掉。
            span_sec = max((t1 - t0).total_seconds(), 1.0)
            local_rate = best_count / span_sec
            ratio = safe_div(local_rate, background)
            if ratio < ratio_threshold:
                continue

            actors = sorted({c.actor_id for c in cluster})
            # 协同层必须校验「跨账号」。一个人连发十八条，那是刷屏，
            # 属于账号层的原创度问题，不是「一群人一起行动」——
            # 不校验的话，任何活跃用户都能把自己的账号刷成「爆发团伙」。
            if len(actors) < min_actors:
                continue

            out.append(
                self.make_hit(
                    f"burst:{topic}:{t0.isoformat()}",
                    Level.STRONG if best_count >= min_items * 2 else Level.WEAK,
                    clamp(0.5 + (ratio - ratio_threshold) / max(ratio_threshold, 1e-6), 0.4, 1.0),
                    (
                        f"「{topic}」在 {span_sec:.0f} 秒内集中出现 {best_count} 条"
                        f"（来自 {len(actors)} 个账号，平均每秒 {local_rate:.2f} 条，"
                        f"是背景速率的 {ratio:.1f} 倍）"
                    ),
                    {
                        "topic": topic,
                        "count": best_count,
                        "span_seconds": round(span_sec, 1),
                        "local_rate": round(local_rate, 4),
                        "background_rate": round(background, 4),
                        "density_ratio": round(ratio, 2),
                        "actors": actors[:30],
                        "actor_count": len(actors),
                    },
                )
            )
        return out


@register
class TextClusterSignal(CoordinationSignal):
    name = "coordination.cluster"
    description = "文本聚类：多条发言互为模板或局部照抄"
    principle = (
        "两段文本的相似度用两级过滤来算，这是性能和准确率的折中："
        "先比 SimHash 的汉明距离（O(1)，用来快速排除绝大多数不相关的对），"
        "通过的再算最长公共**子串**比例（O(nm)，用来确认是真的在照抄）。"
        "只用 SimHash 会漏掉「局部照抄」（改了前半段、原样保留后半段）；"
        "只用最长公共子串则慢到无法接受。"
    )

    def run(self, data: Dataset) -> list[SignalHit]:
        sim_threshold = float(self.cfg.get("similarity", 0.72))
        lcs_threshold = float(self.cfg.get("lcs_ratio", 0.5))
        min_cluster = int(self.cfg.get("min_cluster", 3))
        min_actors = int(self.cfg.get("min_actors", 3))
        min_chars = int(self.cfg.get("min_chars", 8))
        out: list[SignalHit] = []

        for topic, items in data.by_topic().items():
            candidates = [i for i in items if len(i.text.strip()) >= min_chars][:_MAX_PER_TOPIC]
            if len(candidates) < min_cluster:
                continue

            texts = [i.text for i in candidates]
            fingerprints = [
                simhash(tokenize(t, stopwords=self.rules.stopwords)) for t in texts
            ]
            visited = [False] * len(candidates)

            for i in range(len(candidates)):
                if visited[i]:
                    continue
                group = [i]
                for j in range(i + 1, len(candidates)):
                    if visited[j]:
                        continue
                    if hash_similarity(fingerprints[i], fingerprints[j]) < sim_threshold:
                        continue
                    if longest_common_substring_ratio(texts[i], texts[j]) < lcs_threshold:
                        continue
                    group.append(j)

                if len(group) < min_cluster:
                    continue
                for k in group:
                    visited[k] = True

                members = [candidates[k] for k in group]
                actors = sorted({m.actor_id for m in members})
                # 同上：簇里必须来自足够多的**不同账号**。
                # 同一个人重复发同样的话属于账号层（originality），
                # 把它算成「协同」会让单人刷屏污染整个协同层的结论。
                if len(actors) < min_actors:
                    continue
                out.append(
                    self.make_hit(
                        f"cluster:{topic}:{members[0].item_id}",
                        Level.STRONG if len(group) >= min_cluster * 2 else Level.WEAK,
                        clamp(0.5 + 0.1 * len(group), 0.5, 1.0),
                        (
                            f"「{topic}」下有 {len(group)} 条发言高度雷同"
                            f"（来自 {len(actors)} 个账号）"
                        ),
                        {
                            "topic": topic,
                            "size": len(group),
                            "actors": actors[:30],
                            "sample": members[0].preview,
                            "item_ids": [m.item_id for m in members[:30]],
                        },
                    )
                )
        return out


@register
class CoOccurrenceSignal(CoordinationSignal):
    name = "coordination.cooccur"
    description = "共现图：同一批账号反复出现在同一批话题里"
    principle = (
        "把「账号」和「话题」建成二部图，投影到账号一侧得到账号-账号图："
        "两个账号的共同话题越多，说明它们的活动范围重合度越高。"
        "这里刻意加了**交并比**约束，而不只看共同话题的绝对数量 —— "
        "在几个大板块都活跃的正常用户，共同话题数天然就高，只看绝对值会大量误报。"
        "最后用标签传播找社区，得到的是「团伙」而不是「两两相似」。"
    )

    def run(self, data: Dataset) -> list[SignalHit]:
        min_shared = int(self.cfg.get("min_shared", 3))
        min_members = int(self.cfg.get("min_members", 3))
        overlap = float(self.cfg.get("overlap", 0.5))

        actor_topics: dict[str, set[str]] = defaultdict(set)
        for item in data.items:
            if item.topic:
                actor_topics[item.actor_id].add(item.topic)

        actors = sorted(actor_topics)
        adjacency: dict[str, set[str]] = defaultdict(set)
        shared_counts: dict[tuple[str, str], int] = {}

        for a in range(len(actors)):
            for b in range(a + 1, len(actors)):
                left, right = actor_topics[actors[a]], actor_topics[actors[b]]
                shared = left & right
                if len(shared) < min_shared:
                    continue
                union = left | right
                if safe_div(len(shared), len(union)) < overlap:
                    continue  # 活动范围不够重合，认为只是碰巧都在几个大板块活跃
                adjacency[actors[a]].add(actors[b])
                adjacency[actors[b]].add(actors[a])
                shared_counts[(actors[a], actors[b])] = len(shared)

        if not adjacency:
            return []

        labels = _label_propagation(adjacency)
        communities: dict[str, list[str]] = defaultdict(list)
        for node, label in labels.items():
            communities[label].append(node)

        out: list[SignalHit] = []
        for label, members in communities.items():
            if len(members) < min_members:
                continue
            members = sorted(members)
            density = _edge_density(adjacency, members)
            out.append(
                self.make_hit(
                    f"cooccur:{label}",
                    Level.STRONG if len(members) >= min_members * 2 else Level.WEAK,
                    clamp(0.4 + density, 0.4, 1.0),
                    (
                        f"发现 {len(members)} 个账号活动范围高度重合"
                        f"（组内连接密度 {density:.0%}）"
                    ),
                    {
                        "members": members[:30],
                        "member_count": len(members),
                        "density": round(density, 3),
                    },
                )
            )
        return out


def _label_propagation(
    adjacency: dict[str, set[str]], iterations: int = 20
) -> dict[str, str]:
    """标签传播社区发现。

    每个节点先用自己的 id 做标签，每轮把标签改成邻居里最常见的那个，
    直到不再变化。实现只有十几行，但足够把「两两相似」聚成「一群」。

    按 id 排序遍历是为了结果可复现 —— 否则同一份数据两次运行可能给出
    不同的团伙划分，实验就没法对比了。
    """
    labels = {node: node for node in adjacency}
    for _ in range(iterations):
        changed = False
        for node in sorted(adjacency):
            neighbours = adjacency[node]
            if not neighbours:
                continue
            counts = Counter(labels[nb] for nb in neighbours)
            top_count = max(counts.values())
            # 平票时取字典序最小的标签，保证同一份数据两次运行结果一致
            best = min(label for label, count in counts.items() if count == top_count)
            if labels[node] != best:
                labels[node] = best
                changed = True
        if not changed:
            break

    # 收敛后把标签重写为组内最小 id，方便跨运行比对
    grouped: dict[str, list[str]] = defaultdict(list)
    for node, lab in labels.items():
        grouped[lab].append(node)
    final: dict[str, str] = {}
    for members in grouped.values():
        canonical = min(members)
        for node in members:
            final[node] = canonical
    return final


def _edge_density(adjacency: dict[str, set[str]], members: list[str]) -> float:
    """组内实际边数 / 完全图边数。越高说明这群人越「抱团」。"""
    n = len(members)
    if n < 2:
        return 0.0
    member_set = set(members)
    actual = sum(len(adjacency[m] & member_set) for m in members) // 2
    possible = n * (n - 1) // 2
    return safe_div(actual, possible)
