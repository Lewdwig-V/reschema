// rot13.c — argv-driven, 2 functions
#include <stdio.h>
#include <stdint.h>
__attribute__((sysv_abi, noinline)) char rot13_char(char c) {
    if (c >= 'a' && c <= 'z') return (char)('a' + (c - 'a' + 13) % 26);
    if (c >= 'A' && c <= 'Z') return (char)('A' + (c - 'A' + 13) % 26);
    return c;
}
__attribute__((sysv_abi, noinline)) void rot13(char *in_out) {
    for (char *p = in_out; *p; p++) *p = rot13_char(*p);
}
int main(int argc, char **argv) {
    if (argc < 2) { puts("usage: rot13 WORD"); return 2; }
    rot13(argv[1]);
    puts(argv[1]);
    return 0;
}
