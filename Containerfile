FROM docker.io/library/debian:trixie-slim
# One pinned toolchain for everything binary: corpus seeds (gcc+clang matrix) and
# all model compiles (level A + B) happen inside this image, so guest binaries
# carry identical toolchain/libc encodings on every machine (host gcc/glibc
# variance was the source of qiling dirfd-form flakiness; glibc >= 2.41 emits
# the 64-bit form qiling 1.4.6 expects).
RUN apt-get update && apt-get install -y --no-install-recommends gcc clang python3 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /work
# Worker module ships with the repo (mounted ro at run time), not baked in:
# image rebuilds are only for toolchain changes.
CMD ["python3", "-m", "reschema.driver.native_worker"]
