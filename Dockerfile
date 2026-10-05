# syntax=docker/dockerfile:1
#
# Multi-stage build (design D2/D13, design-edges section 7): a builder
# stage installs the locked dependencies and the project itself with uv,
# then the runtime stage copies only the resulting virtual environment
# into a fresh, non-root image. No curl or shell tooling is needed at
# runtime -- the HEALTHCHECK below runs `ecoflow-stats healthcheck`,
# a plain Python HTTP GET against this same container's own /healthz.
#
# NOTE (recorded, not silently skipped): neither base image below is
# pinned to a sha256 digest, as the design asked ("pin the digest when
# implementing") -- this sandbox has no network access to pull and
# verify a real digest. Daniel should pin both before a real build.
# The uv image tag IS a real, verified version: 0.9.10 is the exact uv
# release already installed on this machine (`uv --version`), not an
# invented number -- but it has not been confirmed to exist on the
# registry from here either.

FROM ghcr.io/astral-sh/uv:0.9.10 AS uv

FROM python:3.14-slim-trixie AS builder
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.14-slim-trixie
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
