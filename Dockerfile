# syntax=docker/dockerfile:1
#
# Multi-stage build (design D2/D13, design-edges section 7): a builder
# stage installs the locked dependencies and the project itself with uv,
# then the runtime stage copies only the resulting virtual environment
# into a fresh, non-root image. No curl or shell tooling is needed at
# runtime -- the HEALTHCHECK below runs `ecoflow-stats healthcheck`,
# a plain Python HTTP GET against this same container's own /healthz.
#
# Both base images are pinned to the digest actually resolved and pulled
# during this slice's own `docker build` (design D2/D13: "pin the digest
# when implementing") -- not invented. The uv tag is also the exact uv
# release already installed on this machine (`uv --version`). Re-pin
# both the next time either image is intentionally upgraded.

FROM ghcr.io/astral-sh/uv:0.9.10@sha256:29bd45092ea8902c0bbb7f0a338f0494a382b1f4b18355df5be270ade679ff1d AS uv

FROM python:3.14-slim-trixie@sha256:c3e521df8b2b498a7a682e7e18676771cb80c6b75b8699af886b2d554ce40151 AS builder
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.14-slim-trixie@sha256:c3e521df8b2b498a7a682e7e18676771cb80c6b75b8699af886b2d554ce40151
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data
COPY --from=builder --chown=app:app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1

USER app
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["ecoflow-stats"]
CMD ["serve"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD ["ecoflow-stats", "healthcheck"]
