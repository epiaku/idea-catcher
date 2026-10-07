# The worker image: `catcher worker` plus git (to reach the two repos) and Deno (yt-dlp's JS runtime).
ARG UV_VERSION=0.12.23
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:3.12-slim-bookworm

ARG DENO_VERSION=2.9.7

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client ca-certificates curl unzip bash \
    && rm -rf /var/lib/apt/lists/*

# Deno from the official release zip; pick the build for this CPU (amd64 or arm64).
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
         amd64) target=x86_64-unknown-linux-gnu ;; \
         arm64) target=aarch64-unknown-linux-gnu ;; \
         *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /tmp/deno.zip "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${target}.zip" \
    && unzip -q /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip \
    && deno --version

COPY --from=uv /uv /uvx /usr/local/bin/

RUN git config --system safe.directory '*' \
    && useradd --create-home --uid 1000 --shell /bin/bash catcher \
    && mkdir -p /app /data/repos \
    && chown catcher:catcher /app /data/repos

COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint
COPY scripts/git-askpass.sh /usr/local/bin/git-askpass
RUN chmod 755 /usr/local/bin/docker-entrypoint /usr/local/bin/git-askpass

ENV GIT_ASKPASS=/usr/local/bin/git-askpass \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy \
    PATH=/app/.venv/bin:$PATH

USER catcher
WORKDIR /app

# Dependencies first (cached layer), then the source. The project is installed in place (editable) so
# PROJECT_ROOT, which is src/catcher/core/config.py three levels up, resolves to /app.
COPY --chown=catcher:catcher pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=catcher:catcher src ./src
COPY --chown=catcher:catcher profiles.yaml alembic.ini ./
COPY --chown=catcher:catcher migrations ./migrations
RUN uv sync --frozen --no-dev

ENTRYPOINT ["docker-entrypoint"]
CMD ["catcher", "worker"]
