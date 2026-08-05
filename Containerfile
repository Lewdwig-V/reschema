# One pinned toolchain for everything binary: corpus seeds (gcc+clang matrix) and
# all model compiles (level A + B) happen inside this image, so guest binaries
# carry identical toolchain/libc encodings on every machine (host gcc/glibc
# variance was the source of qiling dirfd-form flakiness; glibc >= 2.41 emits
# the 64-bit form qiling 1.4.6 expects).
# Base image is pinned tag+digest together (rolling trixie stops drifting; the
# digest is load-bearing image identity).
# digest pinned: 2026-08-05
# REFRESH PROCEDURE: (1) podman pull docker.io/library/debian:trixie-slim
# (2) podman image inspect --format '{{.Digest}}' docker.io/library/debian:trixie-slim
# (3) update the digest in FROM and the pin date above
# (4) rebuild localhost/reschema-toolchain:1 and run the full test suite.
FROM docker.io/library/debian:trixie-slim@sha256:38a76d01668772e381ad2826d876627c89e7133e2f8a0f5d567306798b0f2a16
RUN apt-get update && apt-get install -y --no-install-recommends gcc clang python3 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /work
# Worker module ships with the repo (mounted ro at run time), not baked in:
# image rebuilds are only for toolchain changes.
CMD ["python3", "-m", "reschema.driver.native_worker"]
