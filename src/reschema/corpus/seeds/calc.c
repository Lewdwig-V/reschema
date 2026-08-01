// calc.c — multi-function, good level-B source
#include <stdio.h>
#include <stdint.h>
__attribute__((sysv_abi, noinline)) int32_t clamp_i32(int32_t v, int32_t lo, int32_t hi) {
    return v < lo ? lo : v > hi ? hi : v;
}
__attribute__((sysv_abi, noinline)) int32_t sum_range(int32_t lo, int32_t hi) {
    int32_t s = 0;
    for (int32_t i = lo; i <= hi; i++) s = clamp_i32(s + i, -1000, 1000);
    return s;
}
__attribute__((sysv_abi, noinline)) void scale_buf(int32_t *buf, int32_t n, int32_t factor) {
    for (int32_t i = 0; i < n; i++) buf[i] = clamp_i32(buf[i] * factor, -100, 100);
}
int main(void) {
    int32_t data[4] = {1, 2, 3, 4};
    scale_buf(data, 4, 3);
    printf("%d,%d\n", sum_range(-5, 12), data[0]);
    return 0;
}
