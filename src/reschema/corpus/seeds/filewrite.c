// filewrite.c — stdin-driven: transforms stdin bytes, writes them to out.bin,
// reports on stdout. Ground truth for files_written capture + validation;
// the "wb" target is a FIXED relative path (interceptor redirects it in-memory,
// it must never reach the host fs).
#include <stdio.h>
#include <stdint.h>
__attribute__((sysv_abi, noinline)) int32_t xform_byte(int32_t b, int32_t i) {
    return (int32_t)((uint8_t)b ^ (uint8_t)((uint32_t)i * 31u + 7u));
}
int main(void) {
    // ponytail: 4 KiB cap — hidden draws are <=25 bytes; larger inputs truncate
    unsigned char buf[4096];
    size_t n = fread(buf, 1, sizeof buf, stdin);
    uint32_t h = 5381u;
    for (size_t i = 0; i < n; i++) {
        buf[i] = (unsigned char)xform_byte(buf[i], (int32_t)i);
        h = h * 33u + buf[i];
    }
    FILE *f = fopen("out.bin", "wb");
    if (!f) { puts("open failed"); return 2; }
    fwrite(buf, 1, n, f);
    fclose(f);
    printf("%zu bytes -> out.bin djb2=%08x\n", n, h);
    return 0;
}
