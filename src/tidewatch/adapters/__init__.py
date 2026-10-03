"""适配层：把各种来源的数据转成统一的 Dataset。"""

from tidewatch.adapters.base import Adapter, FieldMap, MappedAdapter, dig
from tidewatch.adapters.bilibili import (
    BILIBILI_COMMENT_MAP,
    DOUYIN_COMMENT_MAP,
    BilibiliAdapter,
    parse_danmaku_xml,
)

__all__ = [
    "Adapter",
    "BILIBILI_COMMENT_MAP",
    "BilibiliAdapter",
    "DOUYIN_COMMENT_MAP",
    "FieldMap",
    "MappedAdapter",
    "dig",
    "parse_danmaku_xml",
]
