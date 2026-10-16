# 打包与发行

> 这份文档说明 Windows 免安装版的**怎么打、怎么发、为什么这么设计**。
> 使用者只需要看发行包里的 `使用说明.md`，这份是给改代码的人看的。

---

## 一条命令

```bash
pip install ".[dev]" pyinstaller pillow
python scripts/build_exe.py
```

产出（在 `dist/`）：

```
TideWatch-0.0.1-win64/          ← 免安装目录，解压即用
TideWatch-0.0.1-win64.zip       ← 发给使用者的东西
TideWatch-0.0.1-win64.zip.sha256
```

`make exe` 是它的简写。可用开关：

| 开关 | 作用 |
|---|---|
| `--skip-smoke` | 只构建，不对产物做冒烟（调 spec 时用） |
| `--no-zip` | 不压缩 |
| `--verify-reproducible` | 再打一次包并核对 SHA 一致 |

---

## 发行包的形状

```
TideWatch-0.0.1-win64/
├── TideWatch.exe          主程序（带图标与版本资源）
├── configs/               配置文件 —— 可以直接改
├── rules/                 规则库 —— 可以直接改，这是微调的主要入口
├── data/sample/           样例数据（193 条合成发言）
├── _internal/             Python 运行时 + **同一份资源的兜底副本**
├── README.md              完整文档
├── 使用说明.md            面向使用者的快速上手
└── LICENSE
```

### 为什么是 onedir 而不是 onefile

onefile 每次启动都把自己解压到一个**随机临时目录**，退出即删。
对这个项目来说那不是「慢一点」的问题：

> 它的核心用法是**改 `rules/*.yaml` 微调**。
> 而 onefile 下那些文件每次运行都会被重新解压覆盖 —— 使用者改完，下一次就没了。

分发一个文件夹，代价是压缩包大几十 KB 到几百 KB，换来的是「规则看得见、改得动」。
这笔账很划算。

另外两个附带好处：启动是秒级的（不用解压），排错也直观（文件都在那儿）。

### 为什么资源放两处

`configs/` `rules/` `data/sample/` 各放了两份：exe 同级（主用）与 `_internal/`（兜底）。
加起来 200 KB 左右。

- **同级那份**是使用者实际在改的，也是 `doctor` 里「资源根」默认指向的地方；
- **包内那份**保证「同级目录被删了 / 被杀软清掉了 / 只拷了 exe 和 `_internal`」
  时引擎仍然起得来。

两处不会造成「到底在用哪一份」的困惑：查找顺序是**固定且可打印的**，
`tidewatch doctor` 会明确告诉你当前生效的资源根。

### 查找顺序

```
TIDEWATCH_HOME 环境变量  →  exe 同级目录  →  包内 _internal  →  用户目录
```

判据是「这个目录里有 `configs/` 或 `rules/`」。刻意放宽到「存在」而不是
「`configs/default.yaml` 存在」，因为两种失败方式的好坏完全不同：

| 情况 | 行为 | 为什么 |
|---|---|---|
| 目录**完全不在**（只解压了 exe） | 往下一级找兜底副本 | 让它先能跑起来 |
| 目录**在但不完整**（少了个 yaml） | **停在这里**，由 `doctor` 大声报 fail | 否则会安静地切到 `_internal` 里那份旧副本，使用者改的规则明明不生效、程序却一切正常 —— 这是最难查的一类错 |

---

## 打包时处理好和没处理好的四件事

打包是「问题放大器」：源码里报错是控制台红字，打包后是**双击没反应**。
所以下面四件事在打包这一轮里必须一起做掉。

### 1. 资源路径：`__file__` 会变

源码态 `configs/default.yaml` 是仓库根下的普通文件；冻结后 `__file__` 指向解压目录，
同一个表达式会指到一个不存在的地方 —— **症状不是崩溃，是所有信号权重悄悄回落到默认值**。

判断只在一个模块里做（`src/tidewatch/paths.py`），其它模块不感知自己是不是在 exe 里。
`config.py` 里那句 `Path(__file__).resolve().parents[2]` 已换成 `paths.resource_root()`。

### 2. 控制台编码：样例数据里有 `❤️`

Windows 简体中文环境下，冻结程序的 `stdout` 编码是 GBK，
而样例数据里有 `❤️`（`\u2764`）。实测：

```
$ PYTHONIOENCODING=gbk python -c "print('加薇❤️信')"
UnicodeEncodeError: 'gbk' codec can't encode character '\u2764' in position 2
```

一个工具因为「数据里有 emoji」而崩掉，是最冤的一类事故。
现在 `_setup_console()` 会：把控制台代码页切到 65001（让终端按 UTF-8 渲染）
+ 把 `stdout`/`stderr` 重绑成 UTF-8 且允许替换（编码改不动时不至于崩）。

> 同一个条件下我们的 exe 退出码 0，`❤️` 正常打出来。

### 3. 双击：不能一闪就关

双击 exe 等于「无参数运行」。直接让 argparse 报错退出的话，
新开的控制台会立刻关掉，使用者只看到窗口闪了一下 ——
**这是打包后最糟的失败方式，因为连「哪里错了」都看不到。**

现在会判断「是不是从资源管理器启动的」（用 `GetConsoleProcessList` 看控制台里
是不是只有自己），是的话打印用法提示并等一次回车。
在终端里敲一个光秃秃的 `tidewatch` 不会停 —— 那种情况使用者就在终端前，
停下来等回车只会烦人。

### 4. 异常出口

任何没被预期的异常都会：翻译成人能看懂的一句话 + 落一份崩溃日志到
`%LOCALAPPDATA%\TideWatch\tidewatch-crash.log` + 提示去跑 `doctor`。

翻译刻意先判「缺组件」再判网络/文件类 —— **给一个看起来合理但方向错误的建议，
比不给建议更糟**：使用者会照着一个修不好的方向折腾很久。

---

## 自检（`tidewatch doctor`）

```
[ok] 运行形态      打包 exe
[ok] 程序目录      ...\TideWatch-0.0.1-win64
[ok] 资源根        ...\TideWatch-0.0.1-win64
[ok] 用户目录      C:\Users\<你>\AppData\Local\TideWatch
[ok] 程序目录可写  是 —— configs/ 与 rules/ 可以直接改
[ok] 配置文件      ...\configs\default.yaml
[ok] 配置加载      指纹 c4cea6ed511a
[ok] 规则库        话术 5 组 / 55 条 · 折叠 21 条 · 正则 7 条 · 停用词 119 条
[ok] 信号          注册 10 个 · 启用 10 个
[warn] 中文分词      jieba 未安装 —— 已降级为字符 bigram，功能不受影响
[ok] 样例数据      ...\data\sample\sample.jsonl（193 条）
[ok] 最小端到端    抓到 2 个可疑目标，正常样本未误报

结论：warn —— 可以正常使用，上面标 [warn] 的行只是提示。
```

三档结论 `ok` / `warn` / `fail`，`fail` 时退出码为 1。
**`fail` 时一定会给出可操作的下一步**（「压缩包没解压完整」/「只拷了 exe 出来」）。

「最小端到端」那一行是刻意加的：它真的拿两条探针数据跑一遍完整流水线。
「配置加载成功」和「能出结论」是两件事 —— 空的话术库、权重全部回落默认值、
流水线没接上，这些都能让配置加载成功却什么都判不出来。

`doctor` 还会在检测到「程序目录不可写」时，把默认规则复制到用户目录，
并给出 `TIDEWATCH_HOME` 的用法 —— 发现了问题就顺手给一条能修好的路。

---

## 版本号

**只有一处**：`src/tidewatch/version.py`。

`pyproject.toml`、`configs/default.yaml` 里的 `project.version`、
exe 文件属性里的 `FileVersion`、Release tag、压缩包名 —— 全部从它派生或由它校验。
`tests/test_version.py` 盯着前三者必须一致。

打包后容易出现的最讨厌的一种错，是这五处各写一个数：使用者说「检查更新说有新版，
装完还是旧的」，而这条链路上**一处报错都没有**。

exe 的版本资源是顺手加的，价值在于**不用运行就能核对版本**：
右键 → 属性 → 详细信息里能看到文件版本、产品名、版权。
报问题时说的版本号因此可以被现场核对，而不是只能靠他说。

---

## 校验和的口径

发行说明里的 SHA256 **用来核对下载，不要用来核对构建**。

- **打包这一步是确定的**：`scripts/build_exe.py` 里 zip 是自写的 ——
  文件名排序固定、mtime 写死成 1980-01-01。所以同一份 `dist/` 连打两次 SHA 必然相同
  （`--verify-reproducible` 就是在验证这件事）。
- **PyInstaller 构建这一步不是逐字节确定的**：同一份源码连构建两次，
  产物的 SHA 通常不同。自己重跑一遍构建得到不同的 SHA 是**正常的**。

### `.sha256` 文件的行尾是 LF

这个文件的唯一用途是喂给校验工具，所以它得是标准 `sha256sum` 格式：
`<64 位十六进制><两空格><文件名><LF>`。

行尾必须是 LF，**不能是 CRLF**。构建脚本里 `write_checksum()` 显式写了
`newline="\n"` —— 因为 `Path.write_text` 默认会把 `\n` 翻译成 `os.linesep`，
在 Windows 上写出来就是 CRLF。后果是 Linux / macOS / WSL / Git Bash 上
`sha256sum -c` 全部读不了，只报一句很难看懂的：

```text
sha256sum: 'TideWatch-0.0.1-win64.zip'$'\r': No such file or directory
```

行尾那个 `\r` 被当成了文件名的一部分。

v0.0.1 发出去的 `.sha256` 就是 CRLF 的（`Get-FileHash` + 人工比对不受影响，
所以上面那条口径仍然成立；受影响的是 `sha256sum -c` 这个惯用法）。
`tests/test_build_script.py` 里有一条断言盯的是行尾**字节**，把这个口子堵住了。

---

## CI

`.github/workflows/release.yml` 在推送 `v*` tag 时：

1. 跑 lint + 全量测试
2. 构建发行包（同一条 `scripts/build_exe.py`）
3. 把 zip 与 `.sha256` 附到 GitHub Release

本地构建和 CI 构建走的是同一条脚本 —— 两边不一样的话，
「CI 是绿的」就证明不了发给使用者的那个包是好的。

---

## 已知限制与未验证项

诚实记账，免得日后误以为验过了。

| 项 | 状态 |
|---|---|
| Windows 11 x64 | ✅ 实测构建 + 冒烟通过 |
| Windows 10 | ⚠️ 未测（手上只有一台机器）。PyInstaller 产物对 10/11 兼容，但没有实测证据 |
| 没装 Python 的机器 | ✅ 间接验证：产物自带运行时，冒烟是在不依赖 `.venv` 的子进程里跑的 |
| 只读安装位置（Program Files） | ✅ 实测：用 ACL 拒绝写入 `%TEMP%` 下的副本，`doctor` 正确报 warn 并把默认规则铺到用户目录 |
| 同级资源被删 | ✅ 实测：删掉后自动回落到 `_internal` |
| 同级资源**部分**缺失 | ✅ 实测：停在原地并报 `fail`，不静默切走 |
| 双击不闪退 | ✅ 实测：`Start-Process` 拉起 4 秒后进程仍存活（在等回车） |
| 杀软误报 | ⚠️ 未测（本机没有装第三方杀软）。已关闭 UPX 以降低误报概率；**没有代码签名**，SmartScreen 首次运行会拦，这是免签名程序的常态 |
| 32 位 / ARM64 | ⚠️ 未测。脚本里有平台标签逻辑，但只在 x64 上跑过 |
| macOS / Linux 发行包 | ❌ 未做。本项目只做了 Windows 打包 |
| jieba 可选依赖 | ❌ 发行包刻意不带（省几十 MB）。降级路径由测试守着，但没有在打包版里实测过 |
