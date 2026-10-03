"""JSONL 读写。

统一用 JSONL（每行一个 JSON 对象）作为交换格式，原因是它：
  - 可以流式读，不必把整份数据先载入内存
  - 可以按行 diff，方便在 git 里看数据集的变化
  - 追加一条新样本不需要重写整个文件（标评测集时非常实用）
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

__all__ = ["read_jsonl", "write_jsonl", "append_jsonl"]


def read_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    """逐行读取 JSONL。空行和解析失败的行会被跳过并计数。

    跳过而不是抛异常：手工维护的数据集里出现一行残缺是常事，
    不该让整批导入失败。但会打出一行警告，避免问题被无声吞掉。
    """
    file_path = Path(path)
    bad_lines: list[int] = []
    with file_path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                bad_lines.append(line_no)
                continue
            if isinstance(record, dict):
                yield record
            else:
                bad_lines.append(line_no)
    if bad_lines:
        # 报出具体行号而不是只报个数：手工维护的数据集里，
        # 「跳过了 3 行」帮不上忙，「第 17、42 行有问题」才能直接去改。
        shown = "、".join(str(n) for n in bad_lines[:5])
        if len(bad_lines) > 5:
            print(f"[warn] {file_path.name}: 跳过 {len(bad_lines)} 行无法解析的内容（前几行：{shown}）")
        else:
            print(f"[warn] {file_path.name}: 跳过无法解析的行 {shown}")


def write_jsonl(path: str | Path, records: Iterable[dict[str, Any]]) -> int:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with file_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    return count


def append_jsonl(path: str | Path, records: Iterable[dict[str, Any]]) -> int:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with file_path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    return count
