"""适配层：把任何来源的数据变成引擎认识的 Dataset。

这是「换数据源不用改引擎」的落地位置。核心是 FieldMap —— 一份纯数据的
字段映射表，把原始记录里的路径指到统一模型的字段上。想接一个新平台，
写一份 YAML 映射就行，不需要写代码。

这是继「规则库」之后的第二条微调路径：
  rules/     改判定依据（话术、折叠表、正则）
  adapters/  改数据接入（字段映射）

两条路都不需要读懂引擎内部。
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, fields
from typing import Any

from tidewatch.types import Actor, Dataset, Item
from tidewatch.utils import parse_time

__all__ = ["FieldMap", "Adapter", "MappedAdapter", "dig"]


def dig(record: Any, path: str, default: Any = None) -> Any:
    """按点路径从嵌套结构里取值，支持列表下标。

    `member.uname`、`content.message`、`tags.0` 这类路径都能走。
    任何一段缺失都返回 default，不抛异常 —— 真实数据的字段缺失是常态，
    让一个字段缺失就中断整批导入是不可用的。
    """
    if not path:
        return default
    node = record
    for part in path.split("."):
        if isinstance(node, dict):
            if part not in node:
                return default
            node = node[part]
        elif isinstance(node, (list, tuple)):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return node


@dataclass(slots=True)
class FieldMap:
    """原始记录 -> 统一模型的字段映射。全部用点路径。

    默认值对应本项目自己的规范 JSONL 格式（见 data/sample/）。
    接别的平台时只需要覆盖你关心的那几项。
    """

    item_id: str = "item_id"
    text: str = "text"
    actor_id: str = "actor_id"
    created_at: str = "created_at"
    topic: str = "topic"
    parent_id: str = "parent_id"
    likes: str = "likes"
    mentions: str = "mentions"
    repost_of: str = "repost_of"

    actor_name: str = "actor_name"
    actor_created_at: str = "actor_created_at"
    actor_followers: str = "actor_followers"
    actor_following: str = "actor_following"
    actor_posts: str = "actor_posts"
    actor_has_avatar: str = "actor_has_avatar"
    actor_default_name: str = "actor_default_name"
    actor_verified: str = "actor_verified"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> FieldMap:
        if not data:
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known and isinstance(v, str)})


class Adapter(ABC):
    """适配器基类。"""

    name: str = "adapter"

    @abstractmethod
    def load(self, source: Any) -> Dataset:
        """把来源变成 Dataset。"""


class MappedAdapter(Adapter):
    """按字段映射把一批记录转成 Dataset。

    load() 接受任何可迭代的 dict 序列 —— 列表、生成器、Iterator 都行。
    这样可以边读边转，不必把整份数据先读进内存。
    """

    name = "mapped"

    def __init__(self, field_map: FieldMap | dict[str, Any] | None = None) -> None:
        self.field_map = (
            field_map if isinstance(field_map, FieldMap) else FieldMap.from_dict(field_map)
        )

    def load(self, source: Iterable[dict[str, Any]]) -> Dataset:
        items: list[Item] = []
        actors: dict[str, Actor] = {}
        skipped = 0

        for index, record in enumerate(source):
            if not isinstance(record, dict):
                skipped += 1
                continue
            item = self._to_item(record, index)
            if item is None:
                skipped += 1
                continue
            items.append(item)
            actor = self._to_actor(record)
            if actor is not None and actor.actor_id not in actors:
                actors[actor.actor_id] = actor

        dataset = Dataset(items=items, actors=actors, source=self.name)
        # 把跳过数记在 source 里，方便使用者在日志里发现「我的字段映射写错了」
        if skipped:
            dataset.source = f"{self.name}(skipped={skipped})"
        return dataset

    # ---------------- 内部 ----------------

    def _to_item(self, record: dict[str, Any], index: int) -> Item | None:
        fm = self.field_map
        text = dig(record, fm.text)
        if not isinstance(text, str) or not text.strip():
            return None

        raw_actor = dig(record, fm.actor_id)
        actor_id = str(raw_actor) if raw_actor not in (None, "") else "unknown"

        raw_id = dig(record, fm.item_id)
        item_id = str(raw_id) if raw_id not in (None, "") else self._fallback_id(record, index)

        mentions = dig(record, fm.mentions, ()) or ()
        if isinstance(mentions, str):
            mentions = (mentions,)
        elif not isinstance(mentions, (list, tuple)):
            mentions = ()

        return Item(
            item_id=item_id,
            actor_id=actor_id,
            text=text.strip(),
            created_at=parse_time(dig(record, fm.created_at)),
            topic=str(dig(record, fm.topic, "") or ""),
            parent_id=_optional_str(dig(record, fm.parent_id)),
            mentions=tuple(str(m) for m in mentions),
            repost_of=_optional_str(dig(record, fm.repost_of)),
            likes=_as_int(dig(record, fm.likes, 0)),
        )

    def _to_actor(self, record: dict[str, Any]) -> Actor | None:
        fm = self.field_map
        raw_actor = dig(record, fm.actor_id)
        if raw_actor in (None, ""):
            return None
        return Actor(
            actor_id=str(raw_actor),
            name=str(dig(record, fm.actor_name, "") or ""),
            created_at=parse_time(dig(record, fm.actor_created_at)),
            followers=_as_int(dig(record, fm.actor_followers, 0)),
            following=_as_int(dig(record, fm.actor_following, 0)),
            post_count=_as_int(dig(record, fm.actor_posts, 0)),
            has_avatar=bool(dig(record, fm.actor_has_avatar, True)),
            is_default_name=bool(dig(record, fm.actor_default_name, False)),
            verified=bool(dig(record, fm.actor_verified, False)),
        )

    @staticmethod
    def _fallback_id(record: dict[str, Any], index: int) -> str:
        """记录里没有 id 时，用「话题 + 文本 + 下标」的哈希兜底。

        不用纯下标：那样同一份数据的 id 会随排序变化，评测时对不上。
        加入内容和话题后，同一份数据重复导入能得到相同的 id。
        """
        blob = f"{record.get('topic', '')}|{record.get('text', '')}|{index}"
        return "auto-" + hashlib.blake2b(blob.encode("utf-8"), digest_size=6).hexdigest()


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)
