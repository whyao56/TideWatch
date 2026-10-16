"""命令行入口。

四个子命令：
  scan     扫描一份 JSONL 数据，输出可疑目标与依据
  signals  列出所有已注册的信号及其原理
  rules    查看规则库统计
  doctor   自检：路径、配置、规则库、信号、输出目录，通盘查一遍

所有参数都能被 --set 覆盖，所以做对照实验不需要改代码，也不需要造第二份配置
文件：
    tidewatch scan data.jsonl --set signals.coordination.burst.density_ratio=1.5

打包相关的三件事也在这里，因为「冻结之后行为变了」是使用者最先撞上的问题：

  1. **控制台编码**：源码态在 Windows 终端里能正常打印 emoji，冻结态却可能
     因为 stdout 回落到 GBK 而直接抛 UnicodeEncodeError —— 样例数据里就带 ❤️。
  2. **双击兜底**：双击 exe 等于「无参数运行」，argparse 报错后控制台立刻关掉，
     使用者看到的是「闪了一下就没了」。所以无参数 + 从资源管理器启动时，
     打印引导信息并等一次回车。
  3. **异常出口**：任何没被预期的异常都要留下可读信息 + 一份崩溃日志，
     而不是让窗口一闪而过。
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib.util
import os
import platform
import sys
import traceback
import unicodedata
from pathlib import Path

from tidewatch import paths
from tidewatch.adapters import MappedAdapter
from tidewatch.config import Config
from tidewatch.io import read_jsonl
from tidewatch.pipeline import Pipeline
from tidewatch.report import render_json, render_markdown, render_text, summarize
from tidewatch.rules import RuleBook
from tidewatch.signals import REGISTRY
from tidewatch.types import Level
from tidewatch.version import __version__

__all__ = ["main", "build_parser"]

_LEVELS = {"clean": Level.CLEAN, "weak": Level.WEAK, "strong": Level.STRONG}
_KIND_LABEL = {"text": "文本层", "account": "账号层", "coordination": "协同层"}

# 自检结论的三个档位。刻意只有三档 —— 使用者要判断的是「能不能用」，
# 不是「有多少项不完美」。
_OK, _WARN, _FAIL = "ok", "warn", "fail"
_MARK = {_OK: "[ok]", _WARN: "[warn]", _FAIL: "[fail]"}


# --------------------------------------------------------------------------
# 运行环境的兜底处理
# --------------------------------------------------------------------------


def _setup_console() -> None:
    """把控制台切到 UTF-8，让中文与 emoji 都能打出来。

    冻结之后 stdout 的编码由系统区域设置决定（简体中文 Windows 上是 GBK），
    而样例数据里有 `❤️` 这类字符 —— 一旦报告里带出来就是
    `UnicodeEncodeError` 直接崩掉，且只在打包版复现。

    两步都做：改控制台代码页（让终端按 UTF-8 渲染）+
    重绑流的编码并允许替换（编码不可改时不至于崩）。
    """
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            # 没有控制台（比如被重定向到文件）时这一步本来就会失败，不是错误。
            pass
    for stream in (sys.stdout, sys.stderr):
        # pytest 的 capsys、被重定向到文件的流都没有 reconfigure，静默跳过即可。
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _pause() -> None:
    """双击运行时等一次回车 —— 否则提示信息会随着窗口一起消失。"""
    with contextlib.suppress(EOFError, KeyboardInterrupt):
        input("按回车键关闭这个窗口…")


def _launched_from_explorer() -> bool:
    """判断这次运行是不是「双击 exe」拉起来的。

    判据是 Windows 的控制台进程表：新开一个控制台时里面只有我们自己，
    从已有终端启动时里面至少还有一个父 shell。比「看有没有参数」
    靠谱得多 —— 在终端里敲一个光秃秃的 `tidewatch` 也属于「没有参数」，
    但那时候不该停下来等回车。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        buf = (ctypes.c_uint * 8)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(buf, 8)
        return 0 < count <= 1
    except Exception:
        return False


def _crash_log_path() -> Path:
    return paths.user_root() / "tidewatch-crash.log"


def _write_crash_log(exc: BaseException) -> Path | None:
    """把未预期异常落到用户目录。返回日志路径，写不进去就返回 None。"""
    path = _crash_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        path.write_text(
            f"时间: {stamp}\n"
            f"版本: {__version__}\n"
            f"运行形态: {'打包 exe' if paths.is_frozen() else '源码'}\n"
            f"命令行: {' '.join(sys.argv)}\n"
            f"平台: {platform.platform()}\n"
            f"Python: {sys.version}\n"
            f"资源根: {paths.resource_root()}\n"
            f"\n{traceback.format_exc()}\n",
            encoding="utf-8",
        )
        return path
    except Exception:
        return None


def _friendly_error(exc: BaseException) -> str:
    """把底层异常翻译成「指向能修好的方向」的一句话。

    这里最容易犯的错是给一个看起来合理但方向错误的建议 ——
    比如把缺模块翻译成「检查网络」。所以先判「环境/组件」类，
    再判文件类，最后才兜底。
    """
    name = type(exc).__name__
    if isinstance(exc, ModuleNotFoundError):
        return (
            f"缺少运行组件 {exc.name!r}。这是打包本身的问题，不是使用问题 ——"
            "请重新下载发行包，或改用源码方式运行。"
        )
    if isinstance(exc, UnicodeEncodeError):
        return "输出编码异常。请把控制台代码页切到 UTF-8（chcp 65001）后重试。"
    if isinstance(exc, PermissionError):
        return (
            f"没有权限访问 {exc.filename or '目标路径'}。"
            "如果程序装在 Program Files 这类只读位置，请把压缩包解到"
            "有写权限的目录（例如桌面或 D 盘）后再运行。"
        )
    if isinstance(exc, MemoryError):
        return "内存不足。输入数据可能过大，试试用 --limit 或先按话题拆分数据。"
    if isinstance(exc, OSError):
        return f"系统调用失败（{name}）：{exc}"
    return f"未预期的错误（{name}）：{exc}"


def _diagnose_corpus(root: Path) -> tuple[str, str]:
    """按「文件齐全 / 只缺一部分 / 完全没有」三档给出规则库结论。

    规则文件是按文件粒度加载的，缺一个不会崩 —— 但对应的那一类判断会**静默失效**。
    所以这里必须把它显出来，而不是让它悄悄少一层证据。
    """
    rules_dir = root / "rules"
    required = ("phrases.yaml", "evasion.yaml", "patterns.yaml", "stopwords.txt")
    missing = [name for name in required if not (rules_dir / name).is_file()]

    if len(missing) == len(required):
        return _FAIL, f"规则库整体缺失（找过 {rules_dir}），解压不完整或被人为删掉了"
    if missing:
        return _WARN, f"缺少 {', '.join(missing)} —— 引擎仍可运行，但对应判断会失效"
    return _OK, f"{rules_dir}（4 个文件齐全）"


def _print_usage_hint() -> None:
    print(f"tidewatch {__version__}")
    print()
    print("用法：")
    print("  TideWatch.exe scan 数据.jsonl [--limit 20] [--evidence] [--out 报告.md]")
    print("  TideWatch.exe signals     列出所有信号及其原理")
    print("  TideWatch.exe rules       查看规则库统计")
    print("  TideWatch.exe doctor      自检（路径 / 配置 / 规则 / 输出）")
    print()
    print("第一次用建议先跑 doctor；随包带了一份样例数据，可以直接：")
    print()
    print("  TideWatch.exe scan data\\sample\\sample.jsonl --evidence")


# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------


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
    parser.add_argument(
        "--version",
        action="version",
        version=f"tidewatch {__version__}",
        help="打印版本号后退出",
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
    add("doctor", help="自检运行环境：路径 / 配置 / 规则 / 信号 / 输出")

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


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

# 自检用的最小数据集：一条正常发言 + 一条明显导流。
# 它存在的意义是回答「现在拿一份数据进来，能不能真的出结论」——
# 而不是「库导进来了没有」。这两件事的差集，正是「看着配好了、用起来没反应」
# 的全部来源。
_DOCTOR_PROBE = [
    {
        "item_id": "probe-normal",
        "actor_id": "probe-u1",
        "actor_name": "正常用户",
        "text": "这个思路挺清楚的，回头我也试试看效果怎么样",
        "created_at": 1788000000,
        "topic": "probe",
    },
    {
        "item_id": "probe-spam",
        "actor_id": "probe-u2",
        "actor_name": "用户9472960",
        "text": "需要的话加薇❤️信，详情私聊我，名额有限",
        "created_at": 1788000010,
        "topic": "probe",
        "actor_created_at": 1787999000,
        "actor_has_avatar": False,
        "actor_default_name": True,
        "actor_followers": 0,
        "actor_following": 363,
    },
]


def _display_width(text: str) -> int:
    """按终端里占的列数算宽度 —— 中日韩字符占两列。

    直接用 `ljust` 对不齐：它数的是字符数，而「运行形态」四个字在屏幕上
    占的宽度和「程序目录可写」六个字的宽度差不多。对不齐的自检输出
    看起来就像坏了，所以这里多花几行把宽度算对。
    """
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _doctor(args: argparse.Namespace) -> int:
    """自检。结论三档：ok / warn / fail —— 使用者要知道的是「能不能用」。"""
    results: list[tuple[str, str, str]] = []

    def record(name: str, status: str, detail: str) -> None:
        results.append((name, status, detail))

    print(f"\ntidewatch {__version__} · 自检")
    print(f"Python {platform.python_version()} · {platform.system()} {platform.release()}")
    print()

    # ---- 1. 路径 ----
    for label, value in paths.describe_paths().items():
        record(label, _OK, value)

    root = paths.resource_root()
    app = paths.app_dir()
    app_writable = paths.is_writable(app)
    if app_writable:
        record("程序目录可写", _OK, "是 —— configs/ 与 rules/ 可以直接改")
    else:
        # 程序装在 Program Files 这类只读位置时，使用者仍然需要一份能改的规则。
        # 这里顺手把默认资源铺到用户目录，并告诉他怎么切过去 ——
        # 「发现了问题但没有给出能修好的方向」等于没发现。
        copied = paths.seed_user_root()
        hint = f"已复制默认规则到 {paths.user_root()}" if copied else "请手动复制 configs/ 与 rules/"
        record(
            "程序目录可写",
            _WARN,
            f"否 —— {hint}，然后设 TIDEWATCH_HOME 指向它即可微调",
        )

    # ---- 2. 配置与规则 ----
    config_path = root / "configs" / "default.yaml"
    record(
        "配置文件",
        _OK if config_path.is_file() else _FAIL,
        str(config_path) if config_path.is_file() else f"不存在（找过 {config_path}）",
    )

    rules_status, rules_detail = _diagnose_corpus(root)

    config: Config | None = None
    pipeline: Pipeline | None = None
    try:
        config = Config.load(args.config, args.overrides)
        record("配置加载", _OK, f"指纹 {config.fingerprint()}")
    except FileNotFoundError as exc:
        record("配置加载", _FAIL, str(exc))
    except ValueError as exc:
        record("配置加载", _FAIL, f"配置有误：{exc}")

    if config is not None:
        rules_dir = config.path("corpus.rules_dir", "rules")
        try:
            book = RuleBook.load(rules_dir)
            stats = book.stats()
            detail = (
                f"话术 {stats['phrase_groups']} 组 / {stats['phrases']} 条 · "
                f"折叠 {stats['fold_entries']} 条 · 正则 {stats['regex_rules']} 条 · "
                f"停用词 {stats['stopwords']} 条"
            )
            # 「文件在」不等于「能匹配」。空的话术库不报错，只是什么都不抓，
            # 所以这里额外要求话术条数大于零。
            if stats["phrases"] <= 0:
                rules_status = _FAIL
                detail = "话术库为空 —— 没有任何关键词判断能生效"
            record("规则库", rules_status, f"{detail}；{rules_detail}")
            pipeline = Pipeline(config, book)
        except Exception as exc:  # noqa: BLE001 —— 自检要吞掉一切并如实报告
            record("规则库", _FAIL, f"{type(exc).__name__}: {exc}")
    else:
        record("规则库", rules_status, rules_detail)

    # ---- 3. 信号 ----
    if config is None:
        record("信号", _FAIL, "配置没加载成功，无法判断信号注册情况")
    else:
        enabled = [name for name, cfg in config.iter_signals() if cfg.get("enabled", True)]
        record(
            "信号",
            _OK if len(enabled) >= 8 else _WARN,
            f"注册 {len(REGISTRY)} 个 · 启用 {len(enabled)} 个",
        )

    # ---- 4. 可选依赖 ----
    has_jieba = importlib.util.find_spec("jieba") is not None
    record(
        "中文分词",
        _OK if has_jieba else _WARN,
        "jieba 可用"
        if has_jieba
        else "jieba 未安装 —— 已降级为字符 bigram，功能不受影响（发行包刻意不带它）",
    )

    # ---- 5. 样例数据 ----
    sample = root / "data" / "sample" / "sample.jsonl"
    if sample.is_file():
        try:
            records = sum(1 for _ in read_jsonl(sample))
            record("样例数据", _OK, f"{sample}（{records} 条）")
        except Exception as exc:  # noqa: BLE001
            record("样例数据", _WARN, f"{sample} 读不出来：{exc}")
    else:
        record("样例数据", _WARN, f"没有随包附带（找过 {sample}）")

    # ---- 6. 端到端：真的跑一遍 ----
    if pipeline is not None:
        try:
            probe = MappedAdapter().load(_DOCTOR_PROBE)
            result = pipeline.run(probe)
            flagged = result.flagged()
            bad = [a for a in flagged if a.target == "probe-normal"]
            if not flagged:
                record("最小端到端", _FAIL, "样本里的导流话术一条都没被抓到，引擎可能没在工作")
            elif bad:
                record(
                    "最小端到端",
                    _WARN,
                    f"抓到 {len(flagged)} 个可疑目标，但正常样本也被误报（{bad[0].score:.1f} 分）",
                )
            else:
                record(
                    "最小端到端",
                    _OK,
                    f"抓到 {len(flagged)} 个可疑目标，正常样本未误报",
                )
        except Exception as exc:  # noqa: BLE001
            record("最小端到端", _FAIL, f"{type(exc).__name__}: {exc}")

    # ---- 输出 ----
    width = max(_display_width(name) for name, _, _ in results)
    for name, status, detail in results:
        print(f"{_MARK[status]} {_pad(name, width)}  {detail}")

    worst = _OK
    if any(s == _FAIL for _, s, _ in results):
        worst = _FAIL
    elif any(s == _WARN for _, s, _ in results):
        worst = _WARN

    print()
    if worst == _FAIL:
        print("结论：fail —— 有项目不可用，请先处理上面标 [fail] 的行。")
        print("       最常见的两条原因是：压缩包没有解压完整、只把 exe 单独拷了出来。")
        print("       引擎需要 exe 同级的 configs/ 与 rules/ 一起存在。")
        _print_usage_hint()
    elif worst == _WARN:
        print("结论：warn —— 可以正常使用，上面标 [warn] 的行只是提示。")
    else:
        print("结论：ok —— 一切正常。")
    print()

    return {"ok": 0, "warn": 0, "fail": 1}[worst]


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def _run(argv: list[str]) -> int:
    parser = build_parser()

    if not argv:
        # 双击拉起的 exe 走到这里。直接让 argparse 报错的话，
        # 控制台会「闪一下就关」，使用者只会觉得程序坏了。
        parser.print_help()
        print()
        _print_usage_hint()
        if _launched_from_explorer():
            print()
            _pause()
            return 0
        return 2

    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _doctor(args)

    try:
        pipeline = Pipeline.from_config(args.config, args.overrides)
    except FileNotFoundError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        print("        （用 `tidewatch doctor` 可以看引擎到底去哪里找过。）", file=sys.stderr)
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
            "引擎版本": __version__,
        }
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                render_markdown(result.assessments, meta=meta), encoding="utf-8"
            )
        except OSError as exc:
            print(f"[error] 报告写不进去：{exc}", file=sys.stderr)
            return 2
        print(f"\n报告已写入 {out_path}")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return _run(args)
    except SystemExit:
        # argparse 自己会抛 SystemExit（`--help`、`--version`、用法错误）。
        # 那是正常退出路径，不该被当成崩溃拦下来。
        raise
    except KeyboardInterrupt:
        print("\n[中断] 已取消", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 —— 顶层兜底，不允许出现「一闪就没了」
        print(f"\n[error] {_friendly_error(exc)}", file=sys.stderr)
        log = _write_crash_log(exc)
        if log is not None:
            print(f"[error] 详细堆栈已写入：{log}", file=sys.stderr)
        print("[error] 运行 `tidewatch doctor` 可以看环境哪里不对。", file=sys.stderr)
        if not paths.is_frozen():
            # 源码态直接抛出去，开发时不被这层兜底掩盖问题。
            raise
        if _launched_from_explorer():
            print()
            _pause()
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

