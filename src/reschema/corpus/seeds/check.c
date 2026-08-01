// check.c — stdin-driven crackme, 2 functions
#include <stdio.h>
#include <string.h>
#include <stdint.h>
__attribute__((sysv_abi, noinline)) uint32_t pw_hash(const char *s) {
    uint32_t h = 5381;
    for (; *s; s++) h = h * 33u + (uint8_t)*s;
    return h;
}
__attribute__((sysv_abi, noinline)) int check_pw(const char *s) {
    return pw_hash(s) == 0x1F33E35Fu; /* hash of the real password */
}
int main(void) {
    char buf[64];
    if (!fgets(buf, sizeof buf, stdin)) return 2;
    buf[strcspn(buf, "\n")] = 0;
    if (check_pw(buf)) { puts("OK"); return 0; }
    puts("NOPE");
    return 1;
}
