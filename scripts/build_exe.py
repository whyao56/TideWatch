"""一条命令产出可发行的 Windows 压缩包。

它做四件事，缺一不可：

  1. **构建**：调 PyInstaller（配置在 `packaging/tidewatch.spec`，已进版本库）
  2. **补齐同级资源**：把 `configs/` `rules/` `data/sample/` 和各份文档复制到
     exe 旁边 —— 这是使用者实际会去改的那一份
  3. **对着真实产物做冒烟**：跑 `--version` / `doctor` / `scan`，
     断言的是「产物能干活」，不是「构建返回了 0」
  4. **打包 + 校验和**：确定性 zip（固定时间戳、固定顺序），
     所以同一个 dist 连打两次 SHA 一定相同 —— 印出来的校验和因此
     可以用来核对**下载**是否完整

用法：
    python scripts/build_exe.py                 # 构建 + 冒烟 + 打包
    python scripts/build_exe.py --skip-smoke    # 只构建（调试 spec 时用）
    python scripts/build_exe.py --no-zip
    python scripts/build_exe.py --verify-reproducible   # 额外验证 zip 可复现
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "tidewatch.spec"
DIST = ROOT / "dist"
BUILD = ROOT / "build"

# 固定时间戳：zip 的 mtime 默认取文件系统的改动时间，
# 那会让「同一份 dist 打两次」得到不同的 SHA。写死成 1980-01-01
# （zip 格式能表示的最早时间）之后，打包这一步就完全确定了。
ZIP_STAMP = (1980, 1, 1, 0, 0, 0)

# 阶段标记。CI 上失败时，公开可读的注解只带得住日志尾部几十行 ——
# 有了这些标记，光看尾部就能知道**停在哪一步**，不用把整份日志捞回来。
_STARTED = time.perf_counter()


def stage(name: str) -> None:
    print(f"\n[stage] {name}（累计 {time.perf_counter() - _STARTED:.1f}s）", flush=True)


def _setup_console() -> None:
    """把标准输出切到 UTF-8，让中文提示在任何一台 Windows 上都能打出来。

    这个脚本的每条提示都是中文，而 Windows 上 stdout 的编码由系统区域设置决定：

      - 简体中文机器 → GBK，它**恰好**能编码汉字，所以本地跑永远碰不到问题；
      - 英文机器 → cp1252，连「构建」两个字都编不了，脚本会在第一条中文提示上
        直接崩掉：`UnicodeEncodeError: 'charmap' codec can't encode characters`。

    这就是 v0.0.1 第一次发版失败的原因 —— CI 的 runner 是 en-US。
    「本机能跑」不等于「能跑」，所以这里不依赖区域设置。

    stderr 默认是 backslashreplace，不会崩，但中文报错会变成 `\\u6784\\u5efa`
    这种没法读的东西，所以一起切过去。
    """
    if os.name == "nt":
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            # pytest 的捕获流、被重定向到文件的流没有 reconfigure，静默跳过。
            stream.reconfigure(encoding="utf-8", errors="replace")


def read_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)


def platform_tag() -> str:
    machine = platform.machine().lower()
    bits = 64 if sys.maxsize > 2**32 else 32
    if machine in {"amd64", "x86_64"}:
        return f"win{bits}"
    if machine in {"arm64", "aarch64"}:
        return "winarm64"
    return f"win{bits}"


def run(cmd: list[str], *, timeout: int = 600, **kwargs) -> subprocess.CompletedProcess:
    """跑一条外部命令。默认 10 分钟超时。

    超时是刻意加的：打包后调 exe 时，如果被安全软件拦在扫描环节，
    表现是**挂住**而不是报错。没有超时的话，CI 上会一直跑到 6 小时的作业上限，
    而作业日志只会告诉你「超时了」——又是「看不到原因」的那类问题。
    """
    printable = " ".join(str(part) for part in cmd)
    print(f"$ {printable}", flush=True)
    try:
        return subprocess.run(cmd, check=True, timeout=timeout, **kwargs)  # noqa: S603
    except subprocess.CalledProcessError as exc:
        # CalledProcessError 的默认文本只有「returned non-zero exit status 1」。
        # 而冒烟那几次调用带了 capture_output，子进程的输出被收进 exc 里、
        # 不会印到屏幕上 —— 于是「exe 为什么退出码非 0」恰好变成看不到的那个东西。
        # 这里把它补印出来。PyInstaller 那次没捕获，本来就直通控制台。
        for label, blob in (("stdout", exc.stdout), ("stderr", exc.stderr)):
            if blob:
                print(f"--- 子进程 {label} ---\n{blob}", flush=True)
        raise


# --------------------------------------------------------------------------
# 构建
# --------------------------------------------------------------------------


def build() -> Path:
    if not SPEC.is_file():
        raise SystemExit(f"[error] 找不到 spec：{SPEC}")

    for path in (DIST, BUILD / "pyinstaller"):
        if path.exists():
            shutil.rmtree(path)

    started = time.perf_counter()
    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(DIST),
            "--workpath",
            str(BUILD / "pyinstaller"),
            "--log-level",
            "WARN",
            str(SPEC),
        ]
    )
    elapsed = time.perf_counter() - started
    app = DIST / "TideWatch"
    if not (app / "TideWatch.exe").is_file():
        raise SystemExit("[error] 构建结束但没有产物，检查上面的 PyInstaller 输出")
    print(f"构建完成，用时 {elapsed:.1f}s\n")
    return app


def stage_release(app: Path) -> Path:
    """把 exe 同级该有的东西补齐，并整目录改名成发行名。"""
    version = read_version()
    release = DIST / f"TideWatch-{version}-{platform_tag()}"

    # 同级放一份可编辑的副本。包内（_internal/）也有一份兜底，
    # 但使用者不会去翻 _internal —— 规则放在看得见的地方，是
    # 「改 YAML 就能微调」这个承诺的一部分。
    for name in ("configs", "rules"):
        shutil.copytree(ROOT / name, app / name, dirs_exist_ok=True)
    shutil.copytree(ROOT / "data" / "sample", app / "data" / "sample", dirs_exist_ok=True)

    for name in ("README.md", "LICENSE"):
        shutil.copy2(ROOT / name, app / name)
    shutil.copy2(ROOT / "packaging" / "使用说明.md", app / "使用说明.md")

    if release.exists():
        shutil.rmtree(release)
    app.rename(release)
    return release


# --------------------------------------------------------------------------
# 冒烟：对着真实产物跑
# --------------------------------------------------------------------------


def smoke(release: Path) -> None:
    """三条断言，全部打真实 exe。

    只断言「能干活」的最低线 —— 断言写太死的话，合理的调参会变成红叉，
    最后逼着后人把这套检查删掉，那还不如没有。
    """
    exe = release / "TideWatch.exe"
    version = read_version()

    out = run([str(exe), "--version"], capture_output=True, text=True, encoding="utf-8").stdout
    assert version in out, f"--version 没打出 {version}：{out!r}"
    print(f"  ok  --version → {out.strip()}")

    out = run([str(exe), "doctor"], capture_output=True, text=True, encoding="utf-8").stdout
    assert "结论：" in out, f"doctor 没有给出结论：{out!r}"
    assert "[fail]" not in out, f"doctor 报了 fail：\n{out}"
    assert "打包 exe" in out, f"doctor 没认出自己在跑打包版：\n{out}"
    conclusion = [line for line in out.splitlines() if line.startswith("结论：")][0]
    print(f"  ok  doctor → {conclusion}")

    report = release / "_smoke-report.md"
    out = run(
        [
            str(exe),
            "scan",
            str(release / "data" / "sample" / "sample.jsonl"),
            "--limit",
            "3",
            "--out",
            str(report),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    assert "读取" in out, f"scan 没有读到数据：{out!r}"
    assert "团伙" in out, f"scan 没有给出团伙统计：{out!r}"
    assert report.is_file() and report.stat().st_size > 0, "Markdown 报告没有落盘"
    report.unlink()
    print(f"  ok  scan → {out.strip().splitlines()[0]}")

    # 冻结态最容易崩的地方是编码：样例数据里有 ❤️，而 Windows 控制台默认是 GBK。
    # 上面三次调用都是真实子进程，如果编码没处理好，这里会看到 UnicodeEncodeError。
    print()


# --------------------------------------------------------------------------
# 打包
# --------------------------------------------------------------------------


def zip_dir(src: Path, dst: Path) -> str:
    """确定性打包，返回 zip 的 SHA256。

    顺序按路径排序、mtime 写死 —— 这两点是「同一份目录连打两次 SHA 相同」
    的充分条件。不这么做的话校验和只能核对当次，没法印出来给别人用。
    """
    files = sorted(p for p in src.rglob("*") if p.is_file())
    with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            arcname = (Path(src.name) / path.relative_to(src)).as_posix()
            info = zipfile.ZipInfo(arcname, date_time=ZIP_STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())

    digest = hashlib.sha256(dst.read_bytes()).hexdigest()
    return digest


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def human(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


# --------------------------------------------------------------------------


def main() -> int:
    # 必须是第一件事：下面每一条提示都是中文，而 stdout 的编码在英文 Windows 上
    # 是 cp1252，编不了汉字。放在这里之前，第一条 print 就是崩溃点。
    _setup_console()

    parser = argparse.ArgumentParser(description="构建 TideWatch 的 Windows 发行包")
    parser.add_argument("--skip-smoke", action="store_true", help="跳过对产物的冒烟检查")
    parser.add_argument("--no-zip", action="store_true", help="只构建目录，不压缩")
    parser.add_argument(
        "--verify-reproducible",
        action="store_true",
        help="再打一次包并核对 SHA 一致（证明校验和可用于核对下载）",
    )
    args = parser.parse_args()

    os.chdir(ROOT)
    version = read_version()
    print(f"=== TideWatch {version} · {platform_tag()} ===")
    print(f"Python {platform.python_version()} · {sys.executable}")
    print(f"PyInstaller {_pyinstaller_version()}\n", flush=True)

    stage("PyInstaller 构建")
    app = build()
    stage("补齐同级资源 / 改名成发行目录")
    release = stage_release(app)

    if not args.skip_smoke:
        stage("对真实产物做冒烟")
        smoke(release)

    files = sum(1 for _ in release.rglob("*") if _.is_file())
    print(f"\n发行目录 {release.relative_to(ROOT)}：{files} 个文件 · {human(dir_size(release))}")

    if args.no_zip:
        return 0

    # 注意别用 `with_suffix` —— 目录名里的 `TideWatch-0.0.1-win64` 会被它
    # 当成「后缀是 .1-win64」并替换掉，产出 `TideWatch-0.0.zip` 这种名字。
    archive = release.parent / f"{release.name}.zip"
    if archive.exists():
        archive.unlink()
    stage("确定性打包")
    digest = zip_dir(release, archive)
    print(f"压缩包   {archive.name} · {human(archive.stat().st_size)}")
    print(f"SHA256   {digest}")

    checksum = archive.parent / f"{archive.name}.sha256"
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(f"校验和   {checksum.name}")

    if args.verify_reproducible:
        stage("复核打包可复现")
        probe = archive.parent / f"{archive.name}.probe"
        again = zip_dir(release, probe)
        probe.unlink()
        if again != digest:
            raise SystemExit(
                f"[error] 打包这一步不确定：两次得到 {digest} / {again}。"
                "校验和不能用来核对下载，发行说明里必须写清。"
            )
        print("可复现   两次打包 SHA 一致 ✔")

    print("\n完成。")
    return 0


def _pyinstaller_version() -> str:
    """记下 PyInstaller 的版本。

    CI 里它是即装即用的最新版，本机是当时装下的那一版 —— 两边不一致时，
    「本地能构建、CI 不能」这种事就只是版本差异，而不是代码差异。
    印出来，这一条就不用再靠猜。
    """
    from PyInstaller import __version__

    return __version__


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        print("\n[fail] 构建中断，完整回溯如下：", file=sys.stderr, flush=True)
        traceback.print_exc()
        raise SystemExit(1) from None
