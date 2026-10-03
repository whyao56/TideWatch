"""命令行入口。

三个子命令：
  scan     扫描一份 JSONL 数据，输出可疑目标与依据
  signals  列出所有已注册的信号及其原理
  rules    查看规则库统计

所有参数都能被 --set 覆盖，所以做对照实验不需要改代码，也不需要造第二份配置
文件：
    tidewatch scan data.jsonl --set signals.coordination.burst.density_ratio=1.5
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tidewatch.adapters import MappedAdapter
from tidewatch.io import read_jsonl
from tidewatch.pipeline import Pipeline
from tidewatch.report import render_json, render_markdown, render_text, summarize
from tidewatch.signals import REGISTRY
from tidewatch.types import Level

__all__ = ["main", "build_parser"]

_LEVELS = {"clean": Level.CLEAN, "weak": Level.WEAK, "strong": Level.STRONG}
_KIND_LABEL = {"text": "文本层", "account": "账号层", "coordination": "协同层"}


def _common_options(*, standalone: bool) -> argparse.ArgumentParser:
    """`--config` / `--set` 的定义，同时挂到主解析器和每个子命令上。

    为什么要共享：argparse 默认只接受「全局选项写在子命令之前」，
    于是文档里 `tidewatch scan data.jsonl --set a.b=1` 这种最自然的写法
    其实是不通的。挂到子解析器上就能两个位置都认。

    两个必须踩准的细节：

    1. **子解析器这边默认值必须是 SUPPRESS。** 子命令解析完会把它的命名空间
       合并回主命名空间，如果它自己带一份默认值，就会把父解析器已经解析到的
       `--set` 覆盖成空列表 —— 表现为「选项写在前面反而失效」。

    2. **每个解析器必须各自持有一份独立的选项定义**（所以这里是工厂函数而
       不是共享一个实例）。argparse 的 `parents=` 是共享 action **对象**的，
       而 `set_defaults()` 会就地改写 action 的 `default`。共享同一个对象时，
       给主解析器设默认值会顺手把子解析器的 SUPPRESS 也改掉，第 1 点就破了。
       这个坑很隐蔽：单独看每一行代码都是对的。
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        # 子解析器用 SUPPRESS 表示「我没给这个选项，别往命名空间里写」
        default=None if standalone else argparse.SUPPRESS,
        help="配置文件路径，默认 configs/default.yaml",
    )
    common.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[] if standalone else argparse.SUPPRESS,
        metavar="KEY=VALUE",
        help="临时覆盖配置项，可重复，例如 --set scoring.levels.strong=45",
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tidewatch",
        description="网络水军与协同行为识别引擎",
        parents=[_common_options(standalone=True)],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, **kwargs: object) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[_common_options(standalone=False)], **kwargs)  # type: ignore[arg-type]

    scan = add("scan", help="扫描一份 JSONL 数据")
    scan.add_argument("input", help="输入 JSONL 路径")
    scan.add_argument("--limit", type=int, default=20, help="最多展示多少条，0 表示不限")
    scan.add_argument(
        "--min-level",
        choices=["clean", "weak", "strong"],
        default="weak",
        help="只展示不低于该档位的结果",
    )
    scan.add_argument("--evidence", action="store_true", help="展示每个信号的原始证据")
    scan.add_argument("--json", dest="as_json", action="store_true", help="以 JSON 输出")
    scan.add_argument("--out", default=None, help="把 Markdown 报告写到这个路径")

    add("signals", help="列出所有信号及其原理")
    add("rules", help="查看规则库统计")

    return parser


def _print_signals() -> None:
    by_kind: dict[str, list[type]] = {}
    for cls in REGISTRY.values():
        by_kind.setdefault(cls.kind.value, []).append(cls)

    for kind in ("text", "account", "coordination"):
        classes = by_kind.get(kind)
        if not classes:
            continue
        print(f"\n== {_KIND_LABEL.get(kind, kind)} ==")
        for cls in sorted(classes, key=lambda c: c.name):
            print(f"\n  {cls.name}")
            print(f"    作用   {cls.description}")
            print(f"    原理   {cls.principle}")
            print(f"    目标   {cls.target_kind.value}")
    print()


def _print_rules(pipeline: Pipeline) -> None:
    stats = pipeline.rules.stats()
    print("\n== 规则库 ==")
    print(f"  话术分组   {stats['phrase_groups']} 组 / {stats['phrases']} 条")
    print(f"  折叠映射   {stats['fold_entries']} 条")
    print(f"  正则规则   {stats['regex_rules']} 条")
    print(f"  停用词     {stats['stopwords']} 条")
    print("\n  话术分组明细：")
    for group in pipeline.rules.phrases:
        print(f"    {group.tag:<10} 权重 {group.weight:<4} {len(group.phrases)} 条")
    print()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        pipeline = Pipeline.from_config(args.config, args.overrides)
    except FileNotFoundError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        # 覆盖项写错格式（比如漏了 `=`）属于使用错误。给出人能看懂的提示并退出，
        # 而不是甩一段 traceback —— 用命令行的人不该被迫读源码才知道哪里写错了。
        print(f"[error] 配置有误：{exc}", file=sys.stderr)
        return 2

    if args.command == "signals":
        _print_signals()
        return 0

    if args.command == "rules":
        _print_rules(pipeline)
        return 0

    # ---------------- scan ----------------
    path = Path(args.input)
    if not path.exists():
        print(f"[error] 找不到输入文件: {path}", file=sys.stderr)
        return 2

    data = MappedAdapter().load(read_jsonl(path))
    if not data.items:
        print("[error] 没有读到任何有效发言，请检查字段映射", file=sys.stderr)
        return 2

    result = pipeline.run(data)

    if args.as_json:
        print(render_json(result.flagged(_LEVELS[args.min_level])))
        return 0

    stats = summarize(result.assessments)
    print(
        f"\n读取 {len(data.items)} 条发言 · {len(data.actors)} 个账号 · "
        f"{len(data.by_topic())} 个话题"
    )
    print(
        f"判定对象 {stats['assessed']} 个（其中 {stats['no_signal']} 个未触发任何信号）"
    )
    print(
        f"结果：正常 {stats['clean']} · 弱嫌疑 {stats['weak']} · 强嫌疑 {stats['strong']}"
    )
    print(
        f"      单条 {stats['flagged_by_kind']['item']} · "
        f"账号 {stats['flagged_by_kind']['actor']} · "
        f"团伙 {stats['flagged_by_kind']['group']}"
    )
    print()

    limit = None if args.limit == 0 else args.limit
    print(
        render_text(
            result.assessments,
            limit=limit,
            min_level=_LEVELS[args.min_level],
            show_evidence=args.evidence,
        )
    )

    if args.out:
        out_path = Path(args.out)
        meta = {
            "输入": str(path),
            "发言数": len(data.items),
            "账号数": len(data.actors),
            "话题数": len(data.by_topic()),
            "配置指纹": result.meta["config_fingerprint"],
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            render_markdown(result.assessments, meta=meta), encoding="utf-8"
        )
        print(f"\n报告已写入 {out_path}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
