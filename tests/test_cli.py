"""命令行入口的测试。

CLI 是这个项目对外的主界面 —— 别人拿到仓库，第一件事就是跑它。
所以这里用**真实走一遍**的方式验证（而不是调内部函数），包括输出文件的
落盘：报告渲染里曾经有一个已被改名的统计键，只有真的跑一次才会暴露。
"""

from __future__ import annotations

import json

import pytest

from tidewatch.cli import main

RECORDS = [
    {
        "item_id": "i1",
        "actor_id": "u1",
        "actor_name": "正常用户",
        "text": "这个方案我觉得可以在生产环境里试试看效果",
        "created_at": 1788000000,
        "topic": "v1",
    },
    {
        "item_id": "i2",
        "actor_id": "f1",
        "actor_name": "用户1234567",
        "text": "详情私聊我，加微信谈价格，名额有限速来",
        "created_at": 1788000060,
        "topic": "v1",
        "actor_created_at": 1787999000,
        "actor_has_avatar": False,
        "actor_default_name": True,
        "actor_followers": 0,
        "actor_following": 900,
    },
]


@pytest.fixture
def sample_path(tmp_path):
    path = tmp_path / "records.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in RECORDS), encoding="utf-8"
    )
    return path


# --------------------------------------------------------------------------
# 无数据子命令
# --------------------------------------------------------------------------


def test_signals_command_lists_all_layers(capsys) -> None:
    assert main(["signals"]) == 0
    out = capsys.readouterr().out
    assert "文本层" in out
    assert "账号层" in out
    assert "协同层" in out
    # 原理说明必须打出来 —— 这个项目的一大卖点就是「判定依据是公开可读的」
    assert "原理" in out


def test_rules_command_reports_library_stats(capsys) -> None:
    assert main(["rules"]) == 0
    out = capsys.readouterr().out
    assert "话术分组" in out
    assert "折叠映射" in out
    assert "正则规则" in out


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------


def test_scan_prints_summary_and_findings(sample_path, capsys) -> None:
    assert main(["scan", str(sample_path)]) == 0
    out = capsys.readouterr().out
    assert "读取 2 条发言" in out
    assert "判定对象" in out
    # 那条导流话术必须被抓到，并且给出理由
    assert "加微信" in out or "导流" in out


def test_scan_json_output_is_parseable(sample_path, capsys) -> None:
    assert main(["scan", str(sample_path), "--json", "--limit", "0"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert all({"target", "level", "hits"} <= set(row) for row in payload)


def test_scan_writes_markdown_report(sample_path, tmp_path, capsys) -> None:
    """回归：Markdown 报告曾因为读了一个被改名的统计键而直接抛 KeyError。

    这条测试同时验证「报告真的写到了磁盘上」和「内容不是空的」。
    """
    out_path = tmp_path / "nested" / "report.md"
    assert main(["scan", str(sample_path), "--out", str(out_path)]) == 0
    assert out_path.exists()
    text = out_path.read_text(encoding="utf-8")
    assert text.startswith("# 检测报告")
    # 判定对象 = 2 条发言 + 2 个账号
    assert "判定对象总数：4" in text
    assert sample_path.name in text
    assert "报告已写入" in capsys.readouterr().out


def test_scan_missing_file_returns_error_code(tmp_path, capsys) -> None:
    assert main(["scan", str(tmp_path / "不存在.jsonl")]) == 2
    assert "找不到输入文件" in capsys.readouterr().err


def test_scan_empty_input_returns_error_code(tmp_path, capsys) -> None:
    """空文件不是「没有可疑」而是「没有可用数据」，必须区分开。"""
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert main(["scan", str(path)]) == 2
    assert "没有读到任何有效发言" in capsys.readouterr().err


def test_scan_min_level_strong_suppresses_weak(sample_path, capsys) -> None:
    main(["scan", str(sample_path), "--min-level", "strong", "--limit", "0"])
    strong_only = capsys.readouterr().out
    main(["scan", str(sample_path), "--min-level", "weak", "--limit", "0"])
    weak_and_up = capsys.readouterr().out
    # 放低门槛只会让结果变多，不会变少
    assert weak_and_up.count("──") >= strong_only.count("──")


def test_scan_does_not_flag_normal_account(sample_path, capsys) -> None:
    """回归：正常账号不该出现在「可疑」名单里 —— 规则一松，最先被误伤的是普通用户。"""
    main(["scan", str(sample_path), "--min-level", "weak", "--limit", "0"])
    out = capsys.readouterr().out
    assert "正常用户" not in out


# --------------------------------------------------------------------------
# 配置覆盖
# --------------------------------------------------------------------------


def test_set_override_changes_behaviour(sample_path, tmp_path) -> None:
    """--set 是做对照实验的主要手段，必须真的生效。

    这里关掉文本层的关键词信号：那条导流话术就不再命中，证据行随之消失。
    直接断言「输出变了」而不是断言某个具体分数，避免测试跟着阈值一起漂。
    """
    baseline = tmp_path / "baseline.md"
    assert main(["scan", str(sample_path), "--out", str(baseline)]) == 0
    assert "命中话术" in baseline.read_text(encoding="utf-8")

    muted = tmp_path / "muted.md"
    assert (
        main(
            [
                "scan",
                str(sample_path),
                "--out",
                str(muted),
                "--set",
                "signals.text.keyword.enabled=false",
            ]
        )
        == 0
    )
    assert "命中话术" not in muted.read_text(encoding="utf-8")


def test_bad_override_returns_error_code(capsys) -> None:
    """覆盖项写错格式时要给出人能看懂的提示，而不是甩 traceback。"""
    assert main(["--set", "这不是合法的覆盖项", "signals"]) == 2
    assert "配置有误" in capsys.readouterr().err


@pytest.mark.parametrize("before", [True, False])
def test_set_works_on_both_sides_of_subcommand(sample_path, tmp_path, before) -> None:
    """回归：--set 写在子命令前后都必须生效。

    argparse 默认只认「全局选项在子命令之前」，而最自然的写法恰恰是
    `scan data.jsonl --set a.b=1`。以前这一句会直接报 unrecognized arguments，
    等于文档里给的示例是错的。

    更隐蔽的一半是「写在前面也失效」：`--set` 这个 action 对象被主解析器和
    子解析器共享，给主解析器设默认值会顺手把子解析器的 SUPPRESS 改掉，
    于是子解析器解析完拿一份空列表覆盖回去。所以两个位置都要测。
    """
    out_path = tmp_path / f"muted-{before}.md"
    override = ["--set", "signals.text.keyword.enabled=false"]
    argv = ["scan", str(sample_path), "--out", str(out_path)]
    argv = [*override, *argv] if before else [*argv, *override]
    assert main(argv) == 0
    assert "命中话术" not in out_path.read_text(encoding="utf-8")
