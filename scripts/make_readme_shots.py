"""重新生成 README 里的终端截图。

**为什么要有这个脚本，而不是直接截几张图贴上去：**

README 里那些图是「程序真实输出」的样子。一旦引擎的输出格式或数据变了，图就旧了 ——
而**一张旧的截图比没有截图更糟**：它看起来像证据，但已经不对了。
把生成过程写成脚本，图就变成了**可重新生成的产物**：`python scripts/make_readme_shots.py`
一条命令就能让它们重新和代码一致。

**这些图不是「摆拍」：** 里面的每一行都来自真实执行的命令，脚本只负责画一个终端外框。
字幕（`$ 命令`）就是脚本实际跑的那条命令。

用法：
    python scripts/make_readme_shots.py                    # 输出到 docs/images/
    python scripts/make_readme_shots.py --stage-dir C:\\tw   # 指定 exe 的临时落脚点

渲染需要本机有一个 Chromium 系浏览器（Edge 或 Chrome），只用来把 HTML 画成 PNG。
"""

from __future__ import annotations

import argparse
import contextlib
import html
import os
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "images"

HOST_BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

# ---------------------------------------------------------------- 排版约束
#
# 字体选择不是审美问题，是**对不对得上**的问题。
#
# CLI 的列宽是按「东亚字符占两格」算的（`cli._display_width`），所以终端字体必须满足
#     CJK 宽度 == 2 × 半角宽度
# 常见的编程等宽字体（Cascadia Mono、Consolas）**不含汉字**，回退到微软雅黑之后
# 汉字是 1em 而半角只有 0.6em，比例不是 2:1 —— doctor 那几列会整体错位，
# 看起来像程序坏了。
#
# SimSun / NSimSun 满足这个比例（半角 0.5em、全角 1em），而且它本来就是中文
# Windows 控制台的字体，所以用它既正确又真实。
TERMINAL_FONT = '"NSimSun", "SimSun", "MS Gothic", monospace'
FONT_SIZE = 15.0  # px；半角 = 7.5px，全角 = 15px
LINE_HEIGHT = 1.5
TITLEBAR_H = 34.0
PAD_TOP, PAD_BOTTOM, PAD_X = 14.0, 16.0, 18.0
FRAME = 8.0  # 终端窗体外留的透明边距，给圆角与投影留位置
MAX_IMAGE_W = 880.0  # 超过 GitHub 正文宽度就会被缩到看不清，宁可把字号调小
MIN_FONT_SIZE = 11.5


def display_width(text: str) -> int:
    """按终端口径算显示宽度：东亚宽字符占两格。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


# ---------------------------------------------------------------- 采集真实输出


def _run(cmd: list[str], *, cwd: Path, env_extra: dict[str, str] | None = None) -> list[str]:
    env = None
    if env_extra:
        env = {**os.environ, **env_extra}
    proc = subprocess.run(  # noqa: S603
        cmd,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    text = proc.stdout.replace("\r\n", "\n")
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _source(args: list[str]) -> list[str]:
    return _run([sys.executable, "-m", "tidewatch.cli", *args], cwd=ROOT)


def _staged_exe(stage_dir: Path) -> tuple[Path, Path]:
    """把发行目录复制到一个临时落脚点，返回 (目录, exe)。

    为的是**路径短**。doctor 会打印一堆绝对路径，而仓库路径有六十多个字符 ——
    不换地方的话那一列会撑到 150 列宽，在 GitHub 上被缩放之后谁也看不清。
    换个短目录不是美化，是让这张图真的能被读。
    """
    releases = sorted(p for p in (ROOT / "dist").glob("TideWatch-*-win64") if p.is_dir())
    if not releases:
        raise SystemExit(
            "[error] 没找到发行目录。先跑 `python scripts/build_exe.py`（或 make exe），"
            "或加上 --skip-doctor 只生成源码态的图。"
        )
    release = releases[-1]
    staged = stage_dir / release.name
    if staged.exists():
        shutil.rmtree(staged)
    stage_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(release, staged)
    return staged, staged / "TideWatch.exe"


# ---------------------------------------------------------------- 画图


def _render_html(title: str, prompt: str, lines: list[str], *, font_size: float) -> str:
    body = html.escape("\n".join(lines))
    esc_title = html.escape(title)
    esc_prompt = html.escape(prompt)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
html,body{{margin:0;padding:0;background:transparent}}
.win{{margin:{FRAME}px;background:#0f1216;border:1px solid #262c34;border-radius:10px;
box-shadow:0 2px 8px rgba(0,0,0,.30);overflow:hidden}}
.bar{{height:{TITLEBAR_H - 2:.0f}px;background:#171c22;border-bottom:1px solid #262c34;
display:flex;align-items:center;padding:0 12px;gap:7px}}
.dot{{width:10px;height:10px;border-radius:50%;flex:0 0 auto}}
.r{{background:#ff5f57}}.y{{background:#febc2e}}.g{{background:#28c840}}
.title{{margin-left:8px;color:#8b95a1;font:12px/1 {TERMINAL_FONT};letter-spacing:.02em}}
pre{{margin:0;padding:{PAD_TOP:.0f}px {PAD_X:.0f}px {PAD_BOTTOM:.0f}px;
color:#d7dce3;font:{font_size:.1f}px/{LINE_HEIGHT} {TERMINAL_FONT};
white-space:pre;tab-size:4}}
.p{{color:#7fb88a}}
</style></head><body>
<div class="win">
  <div class="bar"><span class="dot r"></span><span class="dot y"></span><span class="dot g"></span>
    <span class="title">{esc_title}</span></div>
  <pre><span class="p">$ </span>{esc_prompt}
{body}</pre>
</div></body></html>
"""


def _find_browser() -> str:
    for cand in HOST_BROWSERS:
        if Path(cand).is_file():
            return cand
    raise SystemExit(
        "[error] 没找到 Edge / Chrome。这一版只支持用本机的 Chromium 系浏览器渲染 PNG。"
    )


def _screenshot(
    browser: str, html_path: Path, png_path: Path, width: int, height: int, tmp: Path
) -> None:
    base = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--force-device-scale-factor=2",
        f"--user-data-dir={tmp / 'chrome-profile'}",  # 不碰使用者的真实浏览器配置
        f"--window-size={width},{height}",
        f"--screenshot={png_path}",
        html_path.as_uri(),
    ]
    for flags in (base, [*base[:1], "--headless", *base[2:]]):
        with contextlib.suppress(Exception):
            subprocess.run(flags, capture_output=True, timeout=180, check=False)  # noqa: S603
        if png_path.is_file() and png_path.stat().st_size > 0:
            return
    raise SystemExit(f"[error] 渲染失败：{html_path.name}（试过 --headless=new 与 --headless）")


def _draw(
    browser: str, tmp: Path, name: str, title: str, prompt: str, lines: list[str], *, min_cols: int
) -> Path:
    cols = max([display_width(prompt) + 2, min_cols, *(display_width(line) for line in lines)])

    font = FONT_SIZE
    half = font / 2
    if cols * half + 2 * PAD_X + 2 * FRAME > MAX_IMAGE_W:
        font = max(MIN_FONT_SIZE, (MAX_IMAGE_W - 2 * PAD_X - 2 * FRAME) * 2 / cols)
        half = font / 2

    width = int(cols * half + 2 * PAD_X + 2 * FRAME)
    height = int(TITLEBAR_H + PAD_TOP + (len(lines) + 1) * font * LINE_HEIGHT + PAD_BOTTOM + 2 * FRAME + 2)

    html_path = tmp / f"{name}.html"
    html_path.write_text(_render_html(title, prompt, lines, font_size=font), encoding="utf-8")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    png_path = OUT_DIR / f"{name}.png"
    if png_path.exists():
        png_path.unlink()
    _screenshot(browser, html_path, png_path, width, height, tmp)
    print(
        f"  {png_path.relative_to(ROOT)}  {png_path.stat().st_size / 1024:6.1f} KB"
        f"  {cols} 列 {len(lines)} 行  字号 {font:.1f}px"
    )
    return png_path


# ----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="重新生成 README 的终端截图")
    parser.add_argument(
        "--stage-dir",
        default=str(Path(tempfile.gettempdir()) / "tidewatch-readme-shots"),
        help="跑 doctor 时把发行目录复制到哪（路径越短，图越窄）",
    )
    parser.add_argument("--skip-doctor", action="store_true", help="跳过需要发行包的 doctor 图")
    opts = parser.parse_args()

    browser = _find_browser()
    stage_dir = Path(opts.stage_dir)

    scan_cmd = "tidewatch scan data/sample/sample.jsonl"
    routines = [
        (
            "scan",
            "tidewatch — scan",
            scan_cmd,
            # 前 18 行正好覆盖三层：一个团伙命中、一个「只因在团伙里才被拎出来」的账号、
            # 一个文本命中。再往下是同构重复，图会变长而没有新信息。
            _source(["scan", "data/sample/sample.jsonl"])[:18],
            62,
        ),
        (
            "rules",
            "tidewatch — rules",
            "tidewatch rules",
            _source(["rules"]),
            62,
        ),
    ]

    if not opts.skip_doctor:
        _staged, exe = _staged_exe(stage_dir)
        # 把 LOCALAPPDATA 也指到临时目录。doctor 会打印用户目录，不换的话
        # 图上会出现 **本机的 Windows 用户名**（`C:\Users\<谁>`）—— 那是要贴进
        # 公开 README 的东西，不该带上个人标识；顺带那一行也是全图最宽的一行。
        doctor_env = {"LOCALAPPDATA": str(stage_dir / "appdata")}
        routines.insert(
            1,
            (
                "doctor",
                "TideWatch.exe — doctor",
                r".\TideWatch.exe doctor",
                _run([str(exe), "doctor"], cwd=exe.parent, env_extra=doctor_env),
                62,
            ),
        )

    print(f"渲染器 {browser}")
    print(f"输出到 {OUT_DIR.relative_to(ROOT)}/")
    with tempfile.TemporaryDirectory(prefix="tw-shots-") as tmpdir:
        tmp = Path(tmpdir)
        for name, title, prompt, lines, min_cols in routines:
            _draw(browser, tmp, name, title, prompt, lines, min_cols=min_cols)
    print("\n完成。这些图全部来自真实执行，重跑本脚本即可与代码保持一致。")
    return 0


if __name__ == "__main__":
    # 与构建脚本同一条规矩：中文提示在任何一台 Windows 上都得能打出来。
    if os.name == "nt":
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    for _stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
