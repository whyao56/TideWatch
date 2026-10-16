"""资源路径解析的测试。

这一层是打包能不能成立的地基：源码态里 `configs/` 与 `rules/` 是仓库根下的
普通目录，冻结之后它们变成了 exe 同级目录（或包内目录）。路径一旦判断错，
症状不是崩溃，而是「配置加载不到 → 所有信号权重回落到默认值」这种安静的错误。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from tidewatch import paths

REPO = Path(__file__).resolve().parents[1]
RULE_FILES = ("phrases.yaml", "evasion.yaml", "patterns.yaml", "stopwords.txt")


def _make_resource_root(tmp_path: Path, *, rules: tuple[str, ...] = RULE_FILES) -> Path:
    """在临时目录里造一份最小资源根。"""
    root = tmp_path / "res"
    (root / "configs").mkdir(parents=True)
    shutil.copy2(REPO / "configs" / "default.yaml", root / "configs" / "default.yaml")
    (root / "rules").mkdir(parents=True)
    for name in rules:
        shutil.copy2(REPO / "rules" / name, root / "rules" / name)
    return root


def test_tidewatch_home_wins_over_app_dir(tmp_path, monkeypatch) -> None:
    """`TIDEWATCH_HOME` 是逃生口：设了就一定用它，优先于 exe 同级目录。"""
    custom = _make_resource_root(tmp_path)
    monkeypatch.setenv("TIDEWATCH_HOME", str(custom))
    assert paths.resource_root() == custom
    assert paths.user_root() == custom


def test_resource_root_does_not_raise_when_nothing_found(tmp_path, monkeypatch) -> None:
    """都找不到时返回程序目录，而不是在 import 阶段抛异常。

    抛异常的话，报错会出现在一个没有上下文的位置；返回程序目录则能让
    调用方（`Config.load`）给出一句带具体路径的提示。
    """
    monkeypatch.setenv("TIDEWATCH_HOME", str(tmp_path / "空目录"))
    # 程序目录这一层总是排在环境变量之后，所以这里断言的是「函数没有抛」
    got = paths.resource_root()
    assert isinstance(got, Path)


def test_search_locations_is_ordered_and_labelled(tmp_path, monkeypatch) -> None:
    """查找顺序必须稳定、可打印 —— 排错时最想知道的是「你到底去哪找了」。"""
    monkeypatch.setenv("TIDEWATCH_HOME", str(tmp_path))
    labels = [label for label, _ in paths.search_locations()]
    assert "环境变量 TIDEWATCH_HOME" in labels
    assert labels.index("环境变量 TIDEWATCH_HOME") == 0
    assert "用户目录" in labels


def test_seed_user_root_copies_defaults(tmp_path, monkeypatch) -> None:
    bundled = _make_resource_root(tmp_path / "bundle")
    target = tmp_path / "user"
    monkeypatch.setenv("TIDEWATCH_HOME", str(target))
    copied = paths.seed_user_root(bundled)
    assert copied
    assert (target / "configs" / "default.yaml").is_file()
    assert (target / "rules" / "phrases.yaml").is_file()


def test_seed_user_root_never_overwrites_user_edits(tmp_path, monkeypatch) -> None:
    """回归：使用者改过的规则不能被「首次运行」覆盖掉。

    覆盖等于把别人微调好的话术库吃掉，而且不报错 —— 属于最难发现的那类问题。
    """
    bundled = _make_resource_root(tmp_path / "bundle")
    target = tmp_path / "user"
    monkeypatch.setenv("TIDEWATCH_HOME", str(target))
    paths.seed_user_root(bundled)

    edited = target / "rules" / "phrases.yaml"
    edited.write_text("# 我自己改过的规则库\n", encoding="utf-8")

    paths.seed_user_root(bundled)  # 再跑一次，模拟第二次启动
    assert edited.read_text(encoding="utf-8") == "# 我自己改过的规则库\n"


def test_is_writable_detects_read_only_missing_dir(tmp_path) -> None:
    assert paths.is_writable(tmp_path) is True
    assert paths.is_writable(tmp_path / "不存在") is False


def test_is_frozen_is_false_in_source_mode() -> None:
    assert paths.is_frozen() is False


def test_partial_resource_root_is_not_silently_skipped(tmp_path, monkeypatch) -> None:
    """回归：资源目录「在但不完整」时必须停在那里，而不是安静地切到兜底副本。

    「目录完全不在」往下一级找是对的；但「目录在、只是少了文件」还往下找，
    就会让引擎悄悄用上 `_internal` 里的旧副本 —— 使用者改过的规则明明生效不了，
    程序却一切正常。这是最难查的一类错。
    """
    partial = tmp_path / "partial"
    (partial / "rules").mkdir(parents=True)
    monkeypatch.setenv("TIDEWATCH_HOME", str(partial))
    assert paths.resource_root() == partial

    # 只剩下一个空目录、连 configs/ rules/ 都没有 → 才算「完全不在」，才往下找
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("TIDEWATCH_HOME", str(empty))
    assert paths.resource_root() != empty


def test_describe_paths_covers_the_paths_used_for_troubleshooting() -> None:
    described = paths.describe_paths()
    assert set(described) == {"运行形态", "程序目录", "包内解压目录", "资源根", "用户目录"}
    assert described["运行形态"] == "源码"
