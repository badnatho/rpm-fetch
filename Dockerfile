# syntax=docker/dockerfile:1

# rpm-fetch is pure-stdlib Python with zero runtime dependencies, so the image
# is essentially "CPython 3.14 + this package". 3.14 is mandatory: it is the
# first release to ship `compression.zstd`, which is how zstd-compressed
# repodata is read without a third-party wheel.

############################
# Builder — install into an isolated venv
############################
FROM python:3.14-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src

# Copy only what the build needs (pyproject reads README.md for its long
# description). Source is a flat-layout package at the repo root.
COPY pyproject.toml README.md ./
COPY rpm_fetch/ ./rpm_fetch/

# Build + install into /opt/venv so the runtime stage can copy just the venv and
# leave pip/setuptools/build caches behind.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install .

############################
# Runtime — minimal, non-root
############################
FROM python:3.14-slim AS runtime

# No version label here: the version is single-sourced in rpm_fetch/__init__.py
# (CI's metadata-action attaches version labels on tagged builds).
LABEL org.opencontainers.image.title="rpm-fetch" \
      org.opencontainers.image.description="Warm an Artifactory cache from an RPM repository's metadata via HEAD requests." \
      org.opencontainers.image.licenses="GPL-3.0-only"

# GPLv3 requires conveying the license text with the program; an image is a
# distribution, so ship it at the conventional OCI location.
COPY LICENSE /licenses/LICENSE

# PATH points at the copied venv; unbuffered so progress (printed to stderr)
# streams live under `docker logs`; no .pyc writes as a read-only user.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The tool only makes outbound HTTPS calls — it needs nothing on disk, so run it
# as an unprivileged user.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin rpmfetch

COPY --from=builder /opt/venv /opt/venv

USER rpmfetch
WORKDIR /home/rpmfetch

# `docker run <image> <repo-url> [flags]` passes straight through to the CLI;
# `docker run <image>` with no args prints help.
ENTRYPOINT ["rpm-fetch"]
CMD ["--help"]
