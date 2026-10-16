"""自检（doctor）的测试。

自检要回答的是「现在拿一份数据进来，能不能真的出结论」，而不是
「库导进来了没有」。这两件事的差集，正是「看着配好了、用起来没反应」的
全部来源 —— 所以这里既验证它能报 ok，也验证**它能报坏**。

一个只会说 ok 的自检等于没有自检。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tidewatch import paths
from tidewatch.cli import main

REPO = Path(__file__).resolve().parents[1]
RULE_FILES = ("phrases.yaml", "evasion.yaml", "patterns.yaml", "stopwords.txt")


def _resource_root(tmp_path: Path, *, config: bool = True, rules: tuple[str, ...] = RULE_FILES) -> Path:
    root = tmp_path / "res"
    if config:
        (root / "configs").mkdir(parents=True)
        shutil.copy2(REPO / "configs" / "default.yaml", root / "configs" / "default.yaml")
    if rules:
        (root / "rules").mkdir(parents=True)
        for name in rules:
            shutil.copy2(REPO / "rules" / name, root / "rules" / name)
    return root


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    """把资源根指到临时目录，同时保证「用户目录」也在临时目录里。

    关键是别让测试碰真实仓库 —— 否则自检里「顺手把默认规则铺到用户目录」
    那一步会把文件写到开发机的 `%LOCALAPPDATA%` 里。
    """
    root = _resource_root(tmp_path)
    monkeypatch.setenv("TIDEWATCH_HOME", str(root))
    monkeypatch.setattr(paths, "resource_root", lambda: root)
    monkeypatch.setattr(paths, "app_dir", lambda: root)
    return root


def test_doctor_has_no_failures_on_a_complete_tree(fake_root, capsys) -> None:
    """完整的资源树下不允许出现 [fail]。

    刻意不要求「结论：ok」：装了 jieba 与否会让结论在 ok / warn 之间摆动，
    而那是可选依赖，本来就不该影响「能不能用」这个判断。
    真正要守的是**没有任何一项 fail**。
    """
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "自检" in out
    assert "[fail]" not in out
    assert "结论：ok" in out or "结论：warn" in out

    # 版本号必须在自检里出现 —— 这是使用者核对「我装的到底是哪一版」的现场依据
    import tidewatch

    assert tidewatch.__version__ in out


def test_doctor_warns_when_a_rule_file_is_missing(fake_root, tmp_path, capsys) -> None:
    """少一个规则文件不会崩，但对应的判断会静默失效 —— 必须报出来。

    这条测试的价值在于它**能报坏**：全绿的测试证明不了自检是活的。
    """
    (fake_root / "rules" / "patterns.yaml").unlink()
    assert main(["doctor"]) == 0  # warn 不算失败
    out = capsys.readouterr().out
    assert "patterns.yaml" in out
    assert "结论：warn" in out


def test_doctor_fails_when_rules_are_entirely_missing(tmp_path, monkeypatch, capsys) -> None:
    root = _resource_root(tmp_path, rules=())
    monkeypatch.setenv("TIDEWATCH_HOME", str(root))
    monkeypatch.setattr(paths, "resource_root", lambda: root)
    monkeypatch.setattr(paths, "app_dir", lambda: root)
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "规则库整体缺失" in out
    assert "结论：fail" in out


def test_doctor_fails_when_config_is_missing(tmp_path, monkeypatch, capsys) -> None:
    root = _resource_root(tmp_path, config=False)
    monkeypatch.setenv("TIDEWATCH_HOME", str(root))
    monkeypatch.setattr(paths, "resource_root", lambda: root)
    monkeypatch.setattr(paths, "app_dir", lambda: root)
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "找不到配置文件" in out or "不存在" in out
    assert "结论：fail" in out
    # 失败了就要给出可操作的下一步，而不是只报一句「不行」
    assert "scan" in out


def test_doctor_runs_the_engine_end_to_end(fake_root, capsys) -> None:
    """自检必须真的跑一遍最小数据集。

    「配置加载成功」和「能出结论」是两件事：空的话术库、权重全部回落默认值、
    流水线没接上 —— 这些都能让配置加载成功却什么都判不出来。
    """
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "最小端到端" in out
    assert "抓到" in out and "可疑目标" in out
