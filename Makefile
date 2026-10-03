# tidewatch —— 常用命令
#
# 设计意图：把「装环境 → 生成数据 → 跑测试 → 跑评测 → 看报告」这条链路
# 固化成一个字。别人 clone 下来第一件事就是 `make demo`，不该先读三页文档。
#
# Windows 上建议在 Git Bash 或 WSL 里执行；纯 cmd/PowerShell 请用
# 命令右边的等价写法（见 README 的「常见问题」）。

PY ?= python
VENV ?= .venv
DATA ?= data/sample/sample.jsonl
CONFIG ?= configs/default.yaml

ifeq ($(OS),Windows_NT)
	VENV_BIN := $(VENV)/Scripts
else
	VENV_BIN := $(VENV)/bin
endif

.PHONY: help init test lint fmt eval sample scan demo clean freeze

help:
	@echo "make init     创建虚拟环境并安装依赖（含开发依赖）"
	@echo "make test     跑测试"
	@echo "make lint     静态检查"
	@echo "make fmt     自动修复可自动修复的问题"
	@echo "make sample   生成合成数据集（data/sample + data/eval）"
	@echo "make scan     扫描样例数据，打印可疑目标"
	@echo "make eval     跑消融评测，刷新 docs/benchmark.md"
	@echo "make demo     sample -> scan -> eval 一条龙"
	@echo "make clean    清理缓存与产物（不动 data/raw 与 data/sample）"

$(VENV_BIN)/python:
	$(PY) -m venv $(VENV)
	$(VENV_BIN)/python -m pip install --upgrade pip

init: $(VENV_BIN)/python
	$(VENV_BIN)/python -m pip install -e ".[zh,dev]"
	@echo "完成。用 $(VENV_BIN)/python 或先 activate 虚拟环境。"

test:
	$(VENV_BIN)/python -m pytest tests -q

lint:
	$(VENV_BIN)/python -m ruff check src tests scripts eval

fmt:
	$(VENV_BIN)/python -m ruff check src tests scripts eval --fix

sample:
	$(VENV_BIN)/python scripts/gen_sample.py

scan:
	$(VENV_BIN)/python -m tidewatch.cli --config $(CONFIG) scan $(DATA) --limit 15

scan-evidence:
	$(VENV_BIN)/python -m tidewatch.cli --config $(CONFIG) scan $(DATA) --limit 5 --evidence

eval:
	$(VENV_BIN)/python eval/run_eval.py --config $(CONFIG)

demo: sample scan eval
	@echo "演示完毕：结果见 out/ 与 docs/benchmark.md"

# 清理缓存与产物。刻意不碰 data/raw（原始数据可能来之不易）
# 也不碰 data/sample 与 data/eval（review 时要能看到数据长什么样）。
clean:
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
	rm -rf out
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
	find . -name "*.egg-info" -type d -prune -exec rm -rf {} +

freeze:
	$(VENV_BIN)/python -m pip freeze > requirements-lock.txt
	@echo "已写入 requirements-lock.txt（用于复现实验环境）"
