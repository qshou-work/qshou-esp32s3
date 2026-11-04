/*
 * tests/test_runner.h — 极简单元测试框架（host，零依赖）
 * 仅用于 pre_arrival 阶段在 PC 上验证固件纯逻辑；替代方案为 ESP-IDF 的 unity。
 */
#ifndef QSHOU_TEST_RUNNER_H
#define QSHOU_TEST_RUNNER_H

#include <stdio.h>

static int g_tr_pass = 0;
static int g_tr_fail = 0;

#define TR_CHECK(cond) do {                                            \
    if (cond) { g_tr_pass++; }                                          \
    else { g_tr_fail++;                                                 \
           printf("  FAIL %s:%d  (%s)\n", __FILE__, __LINE__, #cond); } \
} while (0)

#define TR_RUN(fn) do {                                                \
    printf("[run] %s\n", #fn);                                          \
    fn();                                                               \
} while (0)

#define TR_SUMMARY() do {                                              \
    printf("\n%d passed, %d failed\n", g_tr_pass, g_tr_fail);          \
    return g_tr_fail ? 1 : 0;                                          \
} while (0)

#endif /* QSHOU_TEST_RUNNER_H */
