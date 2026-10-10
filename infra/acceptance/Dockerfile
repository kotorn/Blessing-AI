# Build only from a minimal context containing this file and the reviewed launcher.
# Supervisor must resolve and independently approve the immutable base/image digest.
ARG ACCEPTANCE_BASE
FROM ${ACCEPTANCE_BASE}
ARG ACCEPTANCE_BASE
ARG LAUNCHER_SHA256
COPY --chmod=0555 local-pilot-container-launcher.mjs /usr/local/bin/blessing-acceptance-check
LABEL org.blessing.acceptance.role="offline-checker" \
  org.blessing.acceptance.base-image="${ACCEPTANCE_BASE}" \
  org.blessing.acceptance.launcher-sha256="${LAUNCHER_SHA256}"
USER 10001:10001
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/blessing-acceptance-check"]
