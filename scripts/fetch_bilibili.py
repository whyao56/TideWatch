#!/usr/bin/env python
"""B站数据抓取示例脚本（**可选**，引擎本身不需要它）。

    python scripts/fetch_bilibili.py --oid 123456 --kind comment --out data/raw/v123.jsonl
    python scripts/fetch_bilibili.py --oid 123456 --kind danmaku --out data/raw/v123-danmaku.xml

════════════════════════════════════════════════════════════════════════
先读这一段，再决定要不要用这个脚本
════════════════════════════════════════════════════════════════════════

**这个脚本不是项目的一部分，它只是一个「怎么写适配层」的示例。**
引擎刻意不内置抓取器，理由有三条，每一条都是真的：

1. **合规风险由使用者承担，不该由工具作者替他决定。**
   批量抓取平台数据同时牵扯《个人信息保护法》、平台用户协议和
   robots 约定。不同用途、不同规模、不同地区的边界完全不一样。
   把抓取器打包进引擎，等于把这份判断藏进一个 `pip install` 里。

2. **抓取方式几天就会失效。** 接口签名、风控策略、Cookie 要求都在变。
   把易碎的部分和稳定的部分分开，引擎才不会被一起拖死。

3. **真实研究场景里你往往已经有数据了。** 官方 API 导出的、
   平台提供的合规数据包、自己账号的备份 —— 这些都直接用适配层读就行，
   根本不需要抓。

所以：**请优先使用官方 API 或你已有的合规数据。** 只有在确认了你的用途
合规、且数据量很小（自用研究、个位数视频）的前提下，再考虑下面的用法。

────────────────────────────────────────────────────────────────────────
使用前的自查清单
────────────────────────────────────────────────────────────────────────
  [ ] 我只抓自己需要的那几个视频/话题，不做全站批量采集
  [ ] 我不抓取、不存储任何可以直接识别到具体自然人的信息
  [ ] 我确认过平台当前的用户协议允许我的用途
  [ ] 抓下来的原始数据不进版本库（.gitignore 已经默认排除 data/raw/）
  [ ] 我不会把抓来的原始文本公开发布

────────────────────────────────────────────────────────────────────────
如果不用这个脚本，怎么拿到数据
────────────────────────────────────────────────────────────────────────
最常见的两条路，都不需要抓：

  A. 官方弹幕接口返回的 XML 直接存盘
     → 用 BilibiliAdapter.load_danmaku() 读，本项目自带解析器
  B. 官方评论接口返回的 JSON 存成 JSONL（每行一个评论对象）
     → 用 BilibiliAdapter.load() 读，字段映射已内置

拿到的文件丢进 data/raw/，然后：

    python -m tidewatch.cli scan data/raw/你的文件.jsonl --evidence

就这样，不需要这个脚本。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tidewatch.adapters.base import dig  # noqa: E402
from tidewatch.adapters.bilibili import parse_danmaku_xml  # noqa: E402
from tidewatch.io import write_jsonl  # noqa: E402

COMMENT_API = "https://api.bilibili.com/x/v2/reply/main"
DANMAKU_API = "https://api.bilibili.com/x/v1/dm/list.so"

# 刻意只给一个很保守的默认值。调大它之前，请回去把上面的自查清单再读一遍。
DEFAULT_PAGES = 1
DEFAULT_DELAY = 3.0  # 秒。请求之间必须留间隔，否则会被风控封禁

UA = "Mozilla/5.0 (compatible; tidewatch-research/0.1; 仅用于小规模研究)"


def fetch(url: str, params: dict[str, str], cookie: str = "", timeout: int = 15) -> bytes:
    """发一个 GET 请求。只支持 GET，因为这个脚本只做「读取」。

    刻意不实现登录、不实现自动翻页到很深、不实现并发 —— 这些能力会让
    「小规模研究」和「批量采集」之间的界线变得模糊。
    """
    from urllib.parse import urlencode

    query = urlencode(params)
    request = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": UA})
    if cookie:
        request.add_header("Cookie", cookie)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


def normalise_comments(payload: bytes, topic: str) -> list[dict]:
    """把官方评论接口的返回转成规范记录。

    这里只做**字段搬运**，不做任何判断 —— 判断是引擎的事。保留 0 值的字段
    也比丢掉好：账号层需要 `followers=0` 这个信息。
    """
    data = json.loads(payload.decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"接口返回错误：code={data.get('code')} message={data.get('message')}")

    replies = dig(data, "data.replies", []) or []
    records: list[dict] = []
    for reply in replies:
        message = dig(reply, "content.message")
        mid = dig(reply, "member.mid")
        if not isinstance(message, str) or not message.strip() or mid is None:
            continue
        records.append(
            {
                "item_id": str(dig(reply, "rpid", "")),
                "actor_id": str(mid),
                "actor_name": str(dig(reply, "member.uname", "") or ""),
                "text": message.strip(),
                "created_at": dig(reply, "ctime"),
                "topic": topic,
                "likes": dig(reply, "like", 0),
                "parent_id": dig(reply, "parent"),
                # 账号资料：有就带上，没有就算了（信号层会按「不表态」处理）
                "actor_has_avatar": bool(dig(reply, "member.avatar", "")),
                "actor_default_name": False,
                "actor_verified": bool(dig(reply, "member.official.verify", 0)),
            }
        )
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="B站数据抓取示例（可选，请先阅读脚本头部说明）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--oid", required=True, help="视频 aid / oid")
    parser.add_argument("--kind", choices=["comment", "danmaku"], default="comment")
    parser.add_argument("--out", required=True, help="输出路径（建议放 data/raw/ 下）")
    parser.add_argument("--cookie", default="", help="可选。涉及登录接口时才需要")
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES, help="最多抓几页")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="请求间隔秒数")
    parser.add_argument("--yes", action="store_true", help="跳过确认（自动化时用）")
    args = parser.parse_args(argv)

    if not args.yes:
        print(
            "\n".join(
                [
                    "",
                    "抓取前请确认：",
                    "  1. 只抓你需要的少数几个视频，不做全站批量采集；",
                    "  2. 不存储任何可直接识别到具体自然人的信息；",
                    "  3. 确认平台当前的用户协议允许你的用途；",
                    "  4. 原始数据留在 data/raw/ 里，不要进版本库、不要公开发布。",
                    "",
                    "更推荐的做法是用官方 API 导出的数据直接喂给适配层，不必抓取。",
                    "",
                ]
            )
        )
        answer = input("确认你的用途合规、且规模很小？输入 yes 继续：").strip().lower()
        if answer != "yes":
            print("已取消。")
            return 1

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        if args.kind == "comment":
            all_records: list[dict] = []
            for page in range(1, max(1, args.pages) + 1):
                payload = fetch(
                    COMMENT_API,
                    {"type": "1", "oid": args.oid, "mode": "3", "next": str(page)},
                    cookie=args.cookie,
                )
                got = normalise_comments(payload, topic=str(args.oid))
                all_records.extend(got)
                print(f"  第 {page} 页：{len(got)} 条")
                if not got:
                    break
                if page < args.pages:
                    time.sleep(max(0.5, args.delay))
            count = write_jsonl(out_path, all_records)
            print(f"已写入 {count} 条评论 -> {out_path}")

        else:
            payload = fetch(DANMAKU_API, {"oid": args.oid}, cookie=args.cookie)
            records = parse_danmaku_xml(payload.decode("utf-8"), topic=str(args.oid))
            count = write_jsonl(out_path, records)
            print(f"已写入 {count} 条弹幕 -> {out_path}")
            print("提示：弹幕记录也可以先存成 .xml 再用 BilibiliAdapter.load_danmaku() 读。")

    except urllib.error.HTTPError as exc:
        print(f"[error] 接口返回 HTTP {exc.code}。可能需要 Cookie，或者被风控了。", file=sys.stderr)
        print("        请先停下来想想是不是抓得太频繁。", file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"[error] 网络异常：{exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    print("\n下一步：")
    print(f"  python -m tidewatch.cli scan {out_path} --evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
