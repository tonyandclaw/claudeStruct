# Multi-stage build producing a single image with all three binaries:
#   /usr/local/bin/cs           (claudestruct, Python)
#   /usr/local/bin/claw-squad   (TypeScript orchestrator)
#   /usr/local/bin/claw-sandbox (Go sandbox wrapper)
#
# Default ENTRYPOINT is `cs`; override with `--entrypoint claw-squad`
# (or `claw-sandbox`) to run the other binaries.

# --- Stage 1: build claw-sandbox (Go) -------------------------------
FROM golang:1.26-alpine AS go-builder
WORKDIR /src
COPY claw-sandbox/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/claw-sandbox .

# --- Stage 2: build claw-squad (TypeScript) -------------------------
#
# Keep this Debian-based so the Node binary copied into the final
# python:slim image uses the same glibc runtime. Alpine's musl-built
# node binary will not run reliably after being copied into Debian.
FROM node:22-bookworm-slim AS ts-builder
WORKDIR /app
RUN npm install -g pnpm@10
COPY claw-squad/package.json claw-squad/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY claw-squad/ ./
RUN pnpm exec tsc
# Trim devDependencies after the build so the runtime image stays small.
RUN pnpm prune --prod

# --- Stage 3: runtime ----------------------------------------------
FROM python:3.12-slim AS runtime

# git is needed for the context-gather step (`cs` shells out to git);
# ca-certificates is needed for HTTPS to the provider APIs; libstdc++6
# is a runtime dependency of the Node binary copied from the builder.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# Node runtime for claw-squad. Pulled from the official Node image rather
# than Debian's older nodejs package.
COPY --from=ts-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=ts-builder /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

# claw-sandbox (Go binary, no runtime deps)
COPY --from=go-builder /out/claw-sandbox /usr/local/bin/claw-sandbox

# claw-squad (compiled JS + production node_modules)
COPY --from=ts-builder /app/dist /opt/claw-squad/dist
COPY --from=ts-builder /app/node_modules /opt/claw-squad/node_modules
COPY --from=ts-builder /app/package.json /opt/claw-squad/package.json
RUN printf '#!/bin/sh\nexec node /opt/claw-squad/dist/cli.js "$@"\n' > /usr/local/bin/claw-squad \
    && chmod +x /usr/local/bin/claw-squad

# claudestruct (`cs`) — install from source so the package metadata matches
# the published wheel format expected by `cs --help`. The container is
# the "batteries included" distribution path for server + local-provider
# use; voice is intentionally omitted because it pulls audio / Whisper
# runtime dependencies that are rarely useful inside a generic container.
WORKDIR /opt/claudestruct
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[server,openai,smart-context,otel,sentry]'

# Run as a non-root user. Most claudestruct workflows operate on a
# bind-mounted repo at /workspace, so make that the working directory.
RUN useradd --create-home --shell /bin/bash claude \
    && mkdir -p /workspace \
    && chown claude:claude /workspace
USER claude
WORKDIR /workspace

LABEL org.opencontainers.image.source="https://github.com/tonyandclaw/claudeStruct"
LABEL org.opencontainers.image.description="claudeStruct: token-efficient Claude Code companion (cs + claw-squad + claw-sandbox)"
LABEL org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["cs"]
CMD ["--help"]
