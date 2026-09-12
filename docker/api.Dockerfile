# OFFLINE 构建版：以现有生产镜像为基底（内网无外网，apt/node 层全部继承）
FROM yuxi-api:0.7.1.beta2
WORKDIR /app

ENV TZ=Asia/Shanghai \
    UV_PROJECT_ENVIRONMENT="/usr/local" \
    UV_COMPILE_BYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    UV_DEFAULT_INDEX=https://pypi.org/simple \
    UV_HTTP_TIMEOUT=180

# 锁文件由 uv 0.12.x 生成，先通过 Nexus 升级 uv 保证兼容
RUN pip install --no-cache-dir uv==0.12.6

COPY backend/pyproject.toml /app/pyproject.toml
COPY backend/.python-version /app/.python-version
COPY backend/uv.lock /app/uv.lock
COPY backend/package /app/package

RUN uv sync --no-cache --group test --no-dev --frozen

COPY backend/server /app/server
COPY docker/api-entrypoint.sh /usr/local/bin/yuxi-entrypoint

RUN chmod 0755 /usr/local/bin/yuxi-entrypoint \
    && mkdir -p /app/runtime /home/yuxi/.cache/rapidocr/models \
    && (chown -R 1000:1000 /app/runtime /home/yuxi || true)

ENV HOME=/home/yuxi
ENV RAPIDOCR_MODEL_DIR=/home/yuxi/.cache/rapidocr/models

USER 1000:1000
ENTRYPOINT ["/usr/local/bin/yuxi-entrypoint"]
