# syntax=docker/dockerfile:1

# Build stage: install with the lockfile and build. Nothing from here ships except the
# standalone server and the static files.
FROM node:22-slim AS build
# NEXT_TELEMETRY_DISABLED: the framework reports anonymous usage to its vendor unless told
# not to. Set here because the build is when it would be sent.
ENV NEXT_TELEMETRY_DISABLED=1 CI=1
RUN corepack enable
WORKDIR /web
# Dependencies first: this layer is reused until the lockfile changes.
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
RUN pnpm build

# Runtime stage: Node, the built server, a non-root user. No package manager, no sources.
FROM node:22-slim AS runtime
RUN useradd --system --uid 10001 app
WORKDIR /web
COPY --from=build --chown=app:app /web/.next/standalone ./
COPY --from=build --chown=app:app /web/.next/static ./.next/static
USER app
ENV NODE_ENV=production PORT=3000 HOSTNAME=0.0.0.0 NEXT_TELEMETRY_DISABLED=1
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:3000/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
CMD ["node", "server.js"]
