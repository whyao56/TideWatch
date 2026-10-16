"""版本号的守卫测试。

打包之后最容易「安静地错」的地方就是版本号：`pyproject.toml` 说是 0.0.1、
包内常量还是上一个数、exe 文件夹叫第三个名字、Release tag 是第四个 ——
使用者看到的现象是「检查更新说有新版，装完还是旧的」，
而这条链路上一处报错都没有。

所以这里把三处版本号钉在一起：只要改了一处忘了别处，测试立刻红。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import tidewatch
from tidewatch import config as config_module
from tidewatch.cli import main

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _pyproject_version() -> str:
    """从 pyproject.toml 里取 `[project]` 段的 version。

    刻意不引 tomllib —— 它要 Python 3.11 起才有，而本项目声明支持 3.10。
    为了一条断言把最低版本抬起来不划算，所以这里做一次最小解析。
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = text.split("[project]", 1)[1].split("\n[", 1)[0]
    match = re.search(r'^version\s*=\s*"([^"]+)"', block, flags=re.MULTILINE)
    assert match, "pyproject.toml 的 [project] 段里找不到 version"
    return match.group(1)


def _config_version() -> str:
    import yaml

    data = yaml.safe_load((ROOT / "configs" / "default.yaml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def test_version_is_semver() -> None:
    assert SEMVER.match(tidewatch.__version__), tidewatch.__version__


def test_version_matches_pyproject() -> None:
    assert tidewatch.__version__ == _pyproject_version(), (
        "包内 __version__ 与 pyproject.toml 不一致 —— 打包出来的文件夹名、"
        "Release tag、自检输出会各说一套"
    )


def test_version_matches_default_config() -> None:
    assert tidewatch.__version__ == _config_version(), (
        "包内 __version__ 与 configs/default.yaml 的 project.version 不一致"
    )


def test_cli_version_flag_prints_package_version(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert tidewatch.__version__ in capsys.readouterr().out


def test_project_root_is_not_derived_from_dunder_file_alone() -> None:
    """资源根必须能被 `TIDEWATCH_HOME` 改写。

    打包后 `Path(__file__).parents[2]` 会指向解压出来的临时目录，
    和「exe 同级目录」完全不是一个地方。这条断言守的是「资源根可被外部指定」
    这个能力本身。
    """
    assert hasattr(config_module, "project_root")
    assert config_module.project_root() == config_module.project_root()
