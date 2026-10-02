# Build only from a minimal context containing this file and the reviewed launcher.
# Supervisor must resolve and independently approve the immutable base/image digest.
ARG ACCEPTANCE_BASE
FROM ${ACCEPTANCE_BASE}
COPY --chmod=0555 local-pilot-container-launcher.mjs /usr/local/bin/blessing-acceptance-check
USER 10001:10001
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/blessing-acceptance-check"]
