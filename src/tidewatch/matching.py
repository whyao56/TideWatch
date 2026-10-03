"""AC 自动机：一次扫描找出文本中所有命中的模式串。

为什么不用逐条正则：话术库动辄几百上千条，`for p in patterns: re.search(p, text)`
是 O(模式数 × 文本长)；AC 自动机把它降到 O(文本长 + 命中数)，**与模式数量无关**。
这是这个项目里少数「换算法带来数量级差异」的地方，也是它值得手写而不是
调库的原因 —— 面试问「你凭什么说它快」时，你能指着 fail 指针讲清楚。

实现要点：
- goto 表用嵌套 dict（稀疏存储，比 256 宽的二维数组省内存）
- fail 指针用 BFS 构建，保证「长后缀的 fail 一定先于短后缀被设置」
- output 在构建时沿 fail 链合并，让搜索阶段命中即取，不做回溯遍历
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

__all__ = ["AhoCorasick", "Match"]


@dataclass(slots=True)
class Match:
    """一次命中。`end` 是开区间，切片用 text[start:end]。"""

    start: int
    end: int
    pattern: str
    tag: str = ""   # 该模式所属的分类标签（如"导流""极端情绪"）

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(slots=True)
class _Node:
    children: dict[str, int] = field(default_factory=dict)
    fail: int = 0
    outputs: list[tuple[str, str]] = field(default_factory=list)


class AhoCorasick:
    """多模式串匹配自动机。

    用法::

        ac = AhoCorasick()
        ac.add("加微信", tag="导流")
        ac.add("私聊", tag="导流")
        ac.build()
        for m in ac.find("有需要加微信详聊"):
            print(m.pattern, m.start, m.tag)
    """

    def __init__(self, *, ignore_case: bool = True) -> None:
        self._nodes: list[_Node] = [_Node()]
        self._built = False
        self._ignore_case = ignore_case
        self._size = 0

    def __len__(self) -> int:
        return self._size

    def _fold(self, text: str) -> str:
        return text.lower() if self._ignore_case else text

    def add(self, pattern: str, tag: str = "") -> AhoCorasick:
        pattern = self._fold(pattern.strip())
        if not pattern:
            return self
        node = 0
        for ch in pattern:
            nxt = self._nodes[node].children.get(ch)
            if nxt is None:
                self._nodes.append(_Node())
                nxt = len(self._nodes) - 1
                self._nodes[node].children[ch] = nxt
            node = nxt
        if not any(p == pattern for p, _ in self._nodes[node].outputs):
            self._nodes[node].outputs.append((pattern, tag))
            self._size += 1
        self._built = False
        return self

    def extend(self, patterns: Iterable[str], tag: str = "") -> AhoCorasick:
        for p in patterns:
            self.add(p, tag)
        return self

    def build(self) -> AhoCorasick:
        """BFS 构建 fail 指针，并把 output 沿 fail 链合并。

        output 合并是关键的优化：不合并的话，搜索时每命中一个节点都要顺着
        fail 链往上走一遍，最坏退化到 O(文本长 × 模式长)。合并之后，
        命中节点的 outputs 天然包含所有后缀模式，取用是 O(1)。
        """
        queue: deque[int] = deque()
        root = self._nodes[0]
        for child in root.children.values():
            self._nodes[child].fail = 0
            queue.append(child)

        while queue:
            node_id = queue.popleft()
            node = self._nodes[node_id]
            for ch, child_id in node.children.items():
                fail_id = node.fail
                while fail_id and ch not in self._nodes[fail_id].children:
                    fail_id = self._nodes[fail_id].fail
                fallback = self._nodes[fail_id].children.get(ch, 0)
                self._nodes[child_id].fail = fallback if fallback != child_id else 0
                inherited = self._nodes[self._nodes[child_id].fail].outputs
                if inherited:
                    known = {p for p, _ in self._nodes[child_id].outputs}
                    self._nodes[child_id].outputs.extend(
                        (p, t) for p, t in inherited if p not in known
                    )
                queue.append(child_id)

        self._built = True
        return self

    def find(self, text: str) -> list[Match]:
        """返回文本中所有命中。重叠命中会全部返回 —— 这里刻意不做去重，
        因为「加微信」和「加薇信」同时命中时，调用方需要知道两条都命中了。"""
        if not self._built:
            self.build()
        if not text:
            return []

        haystack = self._fold(text)
        node = 0
        found: list[Match] = []
        for index, ch in enumerate(haystack):
            while node and ch not in self._nodes[node].children:
                node = self._nodes[node].fail
            node = self._nodes[node].children.get(ch, 0)
            if not node:
                continue
            for pattern, tag in self._nodes[node].outputs:
                found.append(
                    Match(
                        start=index - len(pattern) + 1,
                        end=index + 1,
                        pattern=pattern,
                        tag=tag,
                    )
                )
        return found

    def iter_find(self, text: str) -> Iterator[Match]:
        yield from self.find(text)

    def matched_patterns(self, text: str) -> set[str]:
        return {m.pattern for m in self.find(text)}
