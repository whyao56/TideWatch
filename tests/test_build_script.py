"""scripts/ 下的工具脚本有一条共同约定：提示全是中文，而 stdout 不保证能编码中文。

这条测试守的是一个**只在别人的机器上才会出现**的失败。

Windows 上 stdout 的编码由系统区域设置决定：简体中文机器是 GBK，而 GBK 恰好
能编码汉字 —— 所以本地怎么跑都正常。英文机器是 cp1252，它编不了任何汉字，
脚本会在第一条中文提示上崩掉：

    UnicodeEncodeError: 'charmap' codec can't encode characters

这不是假想。v0.0.1 的第一次发布流水线就是这么挂的：CI 的 runner 是 en-US。
当时公开可读的信息只有一句「Process completed with exit code 1.」，
而本地同条件的完整构建是 EXIT=0 —— 这类问题只能靠把对方的环境搬过来才能看见。

手法就是 `PYTHONIOENCODING=cp1252`，而不是等 CI 报信。
每个脚本都配一条反证：把防护摘掉，探针必须真的崩 ——
否则那条断言可能只是「碰巧为真」，测试会一直绿着而问题还在。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 英文 Windows 的 stdout 编码。挑它是因为它连一个汉字都编不了。
HOSTILE_ENCODING = "cp1252"

# 会打中文的三个脚本。make_icon.py 在模块级就 import PIL，
# 而 CI 的测试作业不装 Pillow（它只是画图标时才需要），所以那一例要跳过。
SCRIPTS = ["build_exe.py", "gen_sample.py", "make_icon.py"]
NEEDS_PIL = {"make_icon.py"}


def _skip_if_dependency_missing(script: str) -> None:
    if script in NEEDS_PIL:
        pytest.importorskip("PIL", reason="make_icon.py 在模块级就 import PIL")

# 探针打印的这几行，正是当初崩掉的那种输出：阶段标记 + 一条完成提示。
_CHINESE_OUTPUT = (
    'print("[stage] PyInstaller 构建（累计 0.5s）")\n'
    'print("构建完成，用时 15.7s")\n'
)

_PROBE = """
import importlib.util, sys

spec = importlib.util.spec_from_file_location("probed", r"{script}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

{guard}

print("hostile-encoding=", sys.stdout.encoding)
{output}
"""

# 接上防护 / 摘掉防护。两句都只是改 module 的属性，其余一切保持原样。
_GUARD_ON = "module._setup_console()"
_GUARD_OFF = "module._setup_console = lambda: None  # 反证"


def _run(program: list[str], *, encoding: str | None) -> subprocess.CompletedProcess[bytes]:
    """带一个指定的 stdout 编码跑子进程。

    不设 PYTHONIOENCODING 时就是本机默认编码 —— 也就是「在中文 Windows 上跑」。
    """
    env = dict(os.environ)
    if encoding is None:
        env.pop("PYTHONIOENCODING", None)
    else:
        env["PYTHONIOENCODING"] = encoding
    return subprocess.run(program, capture_output=True, env=env, check=False)  # noqa: S603


def _text(blob: bytes) -> str:
    """子进程输出按 UTF-8 解；解不出来的字节降级成替换符，别让断言变成解码错误。"""
    return blob.decode("utf-8", errors="replace")


def _probe(script: str, *, guard: str) -> subprocess.CompletedProcess[bytes]:
    driver = _PROBE.format(
        script=ROOT / "scripts" / script,
        guard=guard,
        output=_CHINESE_OUTPUT,
    )
    return _run([sys.executable, "-c", driver], encoding=HOSTILE_ENCODING)


# --------------------------------------------------------------------------
# 真正在流水线里跑的那一个：走完整入口
# --------------------------------------------------------------------------


def test_help_survives_a_console_that_cannot_encode_chinese() -> None:
    """--help 会打出中文的 description 和各选项说明，是最早的中文出口。

    断言退出码为 0 —— 换句话说，构建脚本没有死在半路上。
    这条走的是真正的入口 main()，和 CI 里 `python scripts/build_exe.py` 同一条路。
    """
    proc = _run(
        [sys.executable, str(ROOT / "scripts" / "build_exe.py"), "--help"],
        encoding=HOSTILE_ENCODING,
    )

    assert proc.returncode == 0, (
        f"在 stdout={HOSTILE_ENCODING} 下 --help 没跑完：\n{_text(proc.stderr)}"
    )
    assert "构建 TideWatch 的 Windows 发行包" in _text(proc.stdout)


# --------------------------------------------------------------------------
# 三个脚本共有的那条约定
# --------------------------------------------------------------------------


@pytest.mark.parametrize("script", SCRIPTS)
def test_chinese_output_survives_a_cp1252_console(script: str) -> None:
    """接上防护后，中文必须能打出来。"""
    _skip_if_dependency_missing(script)

    proc = _probe(script, guard=_GUARD_ON)

    assert proc.returncode == 0, (
        f"{script} 在 stdout={HOSTILE_ENCODING} 下没跑完：\n{_text(proc.stderr)}"
    )
    assert "构建完成，用时 15.7s" in _text(proc.stdout)


@pytest.mark.parametrize("script", SCRIPTS)
def test_the_guard_is_actually_what_saves_it(script: str) -> None:
    """反证：摘掉 _setup_console()，同一个探针必须在同一个编码下崩掉。

    没有这一条，上面那条断言可能只是碰巧成立（比如某个 Python 版本忽略了
    PYTHONIOENCODING），测试会永远绿着，而问题依然在。
    """
    _skip_if_dependency_missing(script)

    proc = _probe(script, guard=_GUARD_OFF)
    out, err = _text(proc.stdout), _text(proc.stderr)

    # 先证明「敌意条件」真的成立了。不成立的话，下面那句断言毫无意义。
    assert "hostile-encoding= cp1252" in out, (
        f"PYTHONIOENCODING={HOSTILE_ENCODING} 没生效，这条反证无效：\n{out}"
    )
    assert proc.returncode != 0, f"{script}：摘掉防护之后居然还是跑完了，反证失效：\n{out}"
    assert "UnicodeEncodeError" in err, f"{script}：崩是崩了，但不是编码问题：\n{err}"


@pytest.mark.parametrize("script", SCRIPTS)
def test_setup_console_is_called_before_anything_is_printed(script: str) -> None:
    """_setup_console() 必须是 main() 的第一件事。

    放晚一行都不行：崩溃点就是它前面那句 print。这条断言盯的是位置 ——
    行为测试只证明「这个函数管用」，证明不了「它被挂上了」。
    """
    source = (ROOT / "scripts" / script).read_text(encoding="utf-8")
    # split 之后，第一行是签名那一句的残余（`def main()` 后面的 `) -> None:`），
    # 丢掉它，剩下的才是函数体。
    tail = source.split("def main(", 1)[1]
    body = tail.split("\n", 1)[1]

    first_statement = next(
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    assert first_statement.startswith("_setup_console()"), (
        f"{script}：main() 的第一条语句是 {first_statement!r}，不是 _setup_console()"
    )


def test_chinese_still_prints_on_the_native_console() -> None:
    """本机默认编码下也得正常 —— 修的是「不依赖区域设置」，不是「只在 cp1252 下能跑」。"""
    proc = _probe("build_exe.py", guard=_GUARD_ON)

    assert proc.returncode == 0, _text(proc.stderr)
    assert "构建完成，用时 15.7s" in _text(proc.stdout)


# --------------------------------------------------------------------------
# 校验和文件：它的唯一用途是喂给 `sha256sum -c`
# --------------------------------------------------------------------------


def _load_build_script():
    """把构建脚本当模块载入（不会跑 main —— 它有 __main__ 守卫）。"""
    spec = importlib.util.spec_from_file_location(
        "build_exe_under_test", ROOT / "scripts" / "build_exe.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checksum_file_uses_lf_not_crlf(tmp_path: Path) -> None:
    """行尾必须是 LF。

    Path.write_text 默认把 `\\n` 翻译成 os.linesep，于是在 Windows 上写出来的是
    CRLF —— 而那个文件在 Linux / macOS / WSL / Git Bash 上喂给 `sha256sum -c`
    会失败，报一句看不懂的：

        sha256sum: 'TideWatch-0.0.1-win64.zip'$'\\r': No such file or directory

    行尾的 \\r 被当成了文件名的一部分。这条断言盯的是**字节**，不是内容 ——
    「内容对」和「能用」是两码事，这个缺陷就长在两者的缝里。
    """
    module = _load_build_script()
    archive = tmp_path / "TideWatch-0.0.1-win64.zip"
    # 随便一点字节就行：这条测试只关心随之写出的那个 .sha256 文件。
    archive.write_bytes(b"not really a zip")

    raw = module.write_checksum(archive, "a" * 64).read_bytes()

    assert b"\r" not in raw, f"校验和文件里混进了 CR，各平台的 sha256sum -c 会读不了：{raw!r}"
    assert raw == f"{'a' * 64}  TideWatch-0.0.1-win64.zip\n".encode(), raw
