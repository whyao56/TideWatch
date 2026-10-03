# tidewatch 运行镜像
#
# 刻意用 multi-stage 的最简形态，也刻意不装 jieba：
# 引擎在没有中文分词库时会自动降级为字符 bigram，功能完整、体积小得多。
# 需要更准的分词就自己加一行 pip install jieba。
#
#   docker build -t tidewatch .
#   docker run --rm -v "$PWD/data:/app/data" tidewatch scan data/sample/sample.jsonl
#
# 注意：镜像里**不含** data/raw（原始数据可能含个人信息，见 .dockerignore）。

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先只拷依赖描述，让依赖层能被缓存住 —— 改一行代码不该重装一遍依赖
COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --upgrade pip && python -m pip install -e .

# 规则库与配置是引擎的一部分，必须进镜像；数据不是，靠挂载
COPY configs ./configs
COPY rules ./rules
COPY scripts ./scripts
COPY eval ./eval

# 非 root 运行。这个工具会读外部数据，不该以 root 身份跑
RUN useradd --create-home --uid 10001 tidewatch \
    && mkdir -p /app/data /app/out \
    && chown -R tidewatch:tidewatch /app
USER tidewatch

ENTRYPOINT ["tidewatch"]
CMD ["--help"]
