"""B站与抖音的弹幕 / 评论适配器。

这两个平台的数据有两类常见形态，都**不是**引擎能直接吃的：

  1. 评论 JSON —— 每条评论嵌套 member / content / ctime 等字段
  2. 弹幕 XML —— <d p="时间,模式,字号,颜色,时间戳,池,用户hash,行号">内容</d>

这个模块负责把它们归一化。

关于抓取：本项目**只做解析，不内置抓取器**。原因写在 README 的「合规边界」：
大规模抓取平台数据涉及个人信息与平台协议，风险应由使用者自行评估与承担。
你用官方 API 导出的数据、或者自己的备份数据，都可以直接喂进来。
`scripts/fetch_bilibili.py` 提供了一个最小示例，但它需要你自己提供
Cookie 并自行确认合规性。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from tidewatch.adapters.base import Adapter, FieldMap, MappedAdapter
from tidewatch.types import Dataset

__all__ = ["BilibiliAdapter", "parse_danmaku_xml", "BILIBILI_COMMENT_MAP", "DOUYIN_COMMENT_MAP"]


# B站评论文档结构：
#   {"rpid": ..., "oid": ..., "ctime": ..., "like": ...,
#    "member": {"mid": ..., "uname": ...},
#    "content": {"message": ...}}
BILIBILI_COMMENT_MAP = FieldMap(
    item_id="rpid",
    text="content.message",
    actor_id="member.mid",
    actor_name="member.uname",
    created_at="ctime",
    topic="oid",
    likes="like",
    parent_id="parent",
)


# 抖音评论（第三方工具导出的常见扁平结构，字段名各工具略有差异，
# 这里给的是最常见的一套；对不上时改这里就行，不用动引擎）
DOUYIN_COMMENT_MAP = FieldMap(
    item_id="cid",
    text="text",
    actor_id="user.uid",
    actor_name="user.nickname",
    created_at="create_time",
    topic="aweme_id",
    likes="digg_count",
    parent_id="reply_id",
)


def parse_danmaku_xml(source: str | Path, topic: str = "") -> list[dict[str, Any]]:
    """解析弹幕 XML，返回规范记录。

    B站弹幕的 `p` 属性是一串逗号分隔的值，顺序为：
        出现时间(秒), 模式, 字号, 颜色, 发送时间戳(秒), 池, 用户hash, 行号

    这里只取引擎需要的两个：**发送时间戳**和**用户 hash**。
    发送时间戳是协同层做「时间爆发」检测的基础 —— 也正因为弹幕自带精确到秒
    的时间戳和稳定的用户标识，它比评论区更适合用来验证协同算法。
    """
    if isinstance(source, Path) or (isinstance(source, str) and "<" not in source):
        text = Path(source).read_text(encoding="utf-8")
    else:
        text = str(source)

    root = ET.fromstring(text)
    records: list[dict[str, Any]] = []
    for index, node in enumerate(root.findall(".//d")):
        raw = node.get("p") or ""
        parts = raw.split(",")
        if len(parts) < 7:
            continue
        content = (node.text or "").strip()
        if not content:
            continue
        records.append(
            {
                "item_id": f"dm-{index}",
                "text": content,
                "actor_id": parts[6] or "unknown",
                "created_at": parts[4],
                "topic": topic,
                "likes": 0,
            }
        )
    return records


class BilibiliAdapter(Adapter):
    """B站适配器：同时支持评论 JSON 与弹幕 XML。"""

    name = "bilibili"

    def __init__(self, field_map: FieldMap | None = None) -> None:
        self._mapped = MappedAdapter(field_map or BILIBILI_COMMENT_MAP)

    def load(self, source: Iterable[dict[str, Any]]) -> Dataset:
        """加载评论记录。"""
        dataset = self._mapped.load(source)
        dataset.source = self.name
        return dataset

    def load_danmaku(self, xml_path: str | Path, topic: str = "") -> Dataset:
        """从弹幕 XML 文件加载。"""
        if not topic:
            topic = Path(xml_path).stem
        dataset = self._mapped.load(parse_danmaku_xml(xml_path, topic))
        dataset.source = f"{self.name}:danmaku"
        return dataset
