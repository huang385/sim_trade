FROM python:3.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY requirements.txt ./
COPY private-wheels/ /tmp/private-wheels/

# 两个私有SDK与requirements在同一次解析中安装，避免pip尝试从公共索引下载它们。
# 原始wheel仅存在于builder阶段；最终镜像只接收已安装的Python文件。
RUN set -eux; \
    test "$(find /tmp/private-wheels -maxdepth 1 -type f -name 'ymm_live_data_sdk-0.8.5-*.whl' | wc -l)" -eq 1; \
    test "$(find /tmp/private-wheels -maxdepth 1 -type f -name 'ymm_data_sdk-0.9.4-*.whl' | wc -l)" -eq 1; \
    live_wheel="$(find /tmp/private-wheels -maxdepth 1 -type f -name 'ymm_live_data_sdk-0.8.5-*.whl' -print -quit)"; \
    data_wheel="$(find /tmp/private-wheels -maxdepth 1 -type f -name 'ymm_data_sdk-0.9.4-*.whl' -print -quit)"; \
    test -n "$live_wheel"; \
    test -n "$data_wheel"; \
    python -m pip install --prefix=/install -r requirements.txt "$live_wheel" "$data_wheel"; \
    rm -rf /tmp/private-wheels

FROM python:3.11-slim-bookworm AS runtime

ARG APP_UID=10001
ARG APP_GID=10001

ENV APP_HOME=/opt/sim_trade \
    PATH=/usr/local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "$APP_GID" app \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --home-dir "$APP_HOME" --create-home --shell /usr/sbin/nologin app

COPY --from=builder /install/ /usr/local/

WORKDIR $APP_HOME
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app alembic/ ./alembic/
COPY --chown=app:app app/ ./app/

USER app

EXPOSE 8000 8001

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
