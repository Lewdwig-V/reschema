// pkfmt.c — packed-record runner: the corpus's first "real RE target" class.
// stdin (raw byte domain): [0..1] magic u16 LE 0x5AB1 | [2] version u8 |
// [3] count u8, followed by count records of [tag u8][len u16 LE][payload].
// Exactly count records, no trailing bytes; at most 4096 bytes per packet
// and 1024 bytes per payload. Unknown record tags are skipped by extraction.
// stdout: exit 0 summary / 1 bad magic-or-version / 2 "truncate" (bad shape/size).
//
// A parser workload for future coverage experiments; richer coverage and
// overhead relative to other seeds must be measured after the hook repair.
// The three named helpers are deterministic leaves on the pinned build matrix.
// Buffer helpers require n readable bytes when 4 <= n <= 4096; invalid n is
// rejected before dereference. They update guest .bss pk_errno, which snapshot
// restore includes. They do not validate magic/version (main owns that check).
// Only pk_version_ok is a public function task: the Param API cannot express
// byte buffers. Stripped tasks use fresh VMs; main's libc I/O is not snapshot-safe.
#include <stdio.h>
#include <stdint.h>

#define PK_MAGIC 0x5AB1u
#define PK_MIN_VER 2
#define PK_MAX_VER 4
#define PK_MAX_PACKET 4096
#define PK_MAX_LEN 1024u
#define PK_TAG_SUM 0x20
#define PK_TAG_XOR 0x30

__attribute__((sysv_abi, noinline)) int32_t pk_version_ok(int32_t v) {
    if (v < PK_MIN_VER) return 0;
    if (v > PK_MAX_VER) return 0;
    return v * 16;  // branchy bounds probe, not a bool noop
}

static int pk_errno;

__attribute__((sysv_abi, noinline)) uint32_t pk_extract(unsigned char *buf, int32_t n, int32_t tag) {
    // Keep the parser as a leaf; no callee state is hidden from the opcode scan.
    // errno: 0 success, 1 packet size, 2 record shape/length, 4 requested tag.
    pk_errno = 0;
    uint32_t acc;
    if (tag == PK_TAG_SUM) acc = 0;
    else if (tag == PK_TAG_XOR) acc = 0xA0A0A0A0u;
    else { pk_errno = 4; return 0; }
    if (n < 4 || n > PK_MAX_PACKET) { pk_errno = 1; return 0; }
    int32_t off = 4;
    uint32_t count = buf[3];
    for (uint32_t record = 0; record < count; record++) {
        if (n - off < 3) { pk_errno = 2; return 0; }
        uint32_t t = buf[off];
        uint32_t len = (uint32_t)buf[off + 1] | ((uint32_t)buf[off + 2] << 8);
        off += 3;
        if (len > PK_MAX_LEN || len > (uint32_t)(n - off)) { pk_errno = 2; return 0; }
        if (t == (uint32_t)tag) {
            for (uint32_t i = 0; i < len; i++) acc = (acc ^ buf[off + i]) + len;
        }
        off += (int32_t)len;
    }
    if (off != n) { pk_errno = 2; return 0; }
    return acc;
}

__attribute__((sysv_abi, noinline)) uint32_t pk_checksum(unsigned char *buf, int32_t n) {
    // Hash bytes after the packet header; record validation belongs to extract.
    pk_errno = 0;
    if (n < 4 || n > PK_MAX_PACKET) { pk_errno = 1; return 0; }
    uint32_t h = 2166136261u;
    for (int32_t i = 4; i < n; i++) {
        h ^= buf[i];
        h *= 16777619u;
    }
    return h;
}

int main(void) {
    // One lookahead byte detects oversized input instead of accepting a prefix.
    unsigned char buf[PK_MAX_PACKET + 1];
    size_t n = fread(buf, 1, sizeof buf, stdin);
    if (n < 4 || ((unsigned)buf[0] | ((unsigned)buf[1] << 8)) != PK_MAGIC) {
        puts("bad magic");
        return 1;
    }
    int32_t ok = pk_version_ok(buf[2]);
    if (ok == 0) { puts("bad version"); return 1; }
    if (n > PK_MAX_PACKET) { puts("truncate"); return 2; }
    uint32_t a = pk_extract(buf, (int32_t)n, PK_TAG_SUM);
    int e1 = pk_errno;
    uint32_t b = pk_extract(buf, (int32_t)n, PK_TAG_XOR);
    int e2 = pk_errno;
    if (e1 || e2) { puts("truncate"); return 2; }
    printf("v=%d sum=%u xor-lit=%u fnv=%08lx\n", ok, a, b ^ 0xA0A0A0A0u,
           (unsigned long)pk_checksum(buf, (int32_t)n));
    return 0;
}
