# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir）。

**这个文件要进版本库。** 它就是发行产物的定义 —— 不提交的话，
「这一版 exe 到底是怎么打出来的」只能靠回忆，而回忆不出 exclude 了哪些模块、
隐藏导入了哪些东西。

三个刻意的选择：

1. **onedir 而不是 onefile。** onefile 每次启动都要把自己解压到随机临时目录，
   退出即删。对这个项目来说那不是「慢一点」的问题 —— 它的核心用法是
   **改 `rules/*.yaml` 微调**，而 onefile 下那些文件每次运行都会被重新解压，
   使用者改完下一次就没了。分发一个文件夹，代价是压缩包大几十 KB，
   换来的是「规则看得见、改得动」，这笔账很划算。

2. **资源同时放两处。** `configs/` `rules/` `data/sample/` 会进 `_internal/`
   （兜底），构建脚本还会再往 exe 同级复制一份（主用）。两处加起来才 200 KB
   左右。同级那份是使用者实际在改的，包内那份保证「同级目录被删了 / 被杀软
   清掉了」时引擎仍然起得来。`tidewatch doctor` 会明确打印当前用的是哪一份，
   不会让人猜。

3. **不排除 `jieba`。** 它是可选依赖，装了就装，没装就走降级路径。
   在打包配置里排除它等于单方面替使用者做决定，而「可选」这个词本身就是
   承诺。发行包刻意不带它 —— 省下几十 MB，而且降级路径有测试守着。
"""

import re
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

ROOT = Path(SPECPATH).parent  # noqa: F821 —— SPECPATH 由 PyInstaller 注入
SRC = ROOT / "src"
APP = "TideWatch"

# 版本号从 pyproject.toml 读，不在 spec 里另写一份 ——
# 两处版本号迟早会不一致，而症状是「属性里显示的版本和自检说的不一样」。
VERSION = re.search(
    r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M
).group(1)
VERSION_PARTS = [int(part) for part in VERSION.split(".")] + [0]


def _version_resource() -> VSVersionInfo:
    """写进 exe 的版本资源。

    它的价值是「不用运行就能核对版本」：右键 → 属性 → 详细信息里能看到文件版本、
    产品名、版权。使用者报问题时说的版本号，因此可以被现场核对，
    而不是只能靠他说。
    """
    return VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=tuple(VERSION_PARTS[:4]),
            prodvers=tuple(VERSION_PARTS[:4]),
            mask=0x3F,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "080404B0",  # 简体中文
                        [
                            StringStruct("CompanyName", "tidewatch contributors"),
                            StringStruct("FileDescription", "TideWatch · 观潮 —— 网络水军与协同行为识别引擎"),
                            StringStruct("FileVersion", VERSION),
                            StringStruct("InternalName", APP),
                            StringStruct("LegalCopyright", "MIT License"),
                            StringStruct("OriginalFilename", f"{APP}.exe"),
                            StringStruct("ProductName", "TideWatch"),
                            StringStruct("ProductVersion", VERSION),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [2052, 1200])]),  # 2052 = zh-CN
        ],
    )


a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[
        (str(ROOT / "configs"), "configs"),
        (str(ROOT / "rules"), "rules"),
        (str(ROOT / "data" / "sample"), "data/sample"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 不排除任何东西。这个项目的依赖只有 PyYAML，体积本来就不是问题，
    # 而 exclude 是「静默失败」的常见来源 —— 被排掉的模块要到运行时才报错。
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # 关闭 UPX：压缩省不了多少体积，却显著提高被杀软误报的概率
    # （自解压壳是恶意软件的常见特征）。第一版不值得为几十 MB 冒这个险。
    upx=False,
    console=True,  # 命令行工具必须留控制台，否则报错会变成「双击没反应」
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "packaging" / "tidewatch.ico"),
    version=_version_resource(),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP,
)
