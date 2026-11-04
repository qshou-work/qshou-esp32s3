#!/usr/bin/env bash
# precheck-seams.sh —— 零硬件交叉编译预检
#
# 目的：在不烧机、不跑全量构建的前提下，用 ESP-IDF 原生编译标志
#       对「host 接缝不覆盖的那些文件」做语法预检。
#
# 背景：host 构建只会编译 platform/host/*.c，platform/esp32/*.c 完全
#       不参与编译。这意味着 ESP-IDF API 误用（如宏改名）在 host 构建
#       里永远暴露不出来，只会在真机烧写当天引爆。本脚本专门堵这个洞。
#
# 用法：
#   ./precheck-seams.sh <build-dir>
#   build-dir 需为已完成一次 idf.py build 的目录（提供 compile_commands.json）
#
# 依赖：xtensa-esp32s3-elf-gcc、python（用于提取编译参数）
#
# ── 关于桩头（stub）─────────────────────────────────────────────────
#   esp32-camera 是**第三方托管组件**（需联网 add-dependency 拉取），
#   零硬件环境下往往不在。为仍能对 platform_camera.c 做类型/语法检查，
#   在 build/_precheck/stub/ 下放置最小桩头（字段与 API 签名照实摘录）。
#   本脚本 A 段对每个文件先按「无桩头」编译；若失败原因只是缺少某个
#   第三方组件头（*.h: No such file），再带桩头目录重试一次，并在结果
#   行标注 (stub) 以示区分。真组件存在时不会走到 stub 分支。
#
#   ⚠ 桩头不等于真头：桩头通过 ≠ 真机可用。仅用于抓语法/类型/宏误用。

set -u

BUILD_DIR="${1:-build}"
FW_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STUB_DIR="$BUILD_DIR/_precheck/stub"

# 交叉编译器：优先环境变量 XTGCC，否则自动探测 ~/.espressif 下的版本目录
if [ -z "${XTGCC:-}" ]; then
    XTGCC=$(ls -1 "$HOME"/.espressif/tools/xtensa-esp-elf/*/xtensa-esp-elf/bin/xtensa-esp32s3-elf-gcc.exe 2>/dev/null | sort -V | tail -1)
fi
if [ -z "${XTGCC:-}" ] || [ ! -x "$XTGCC" ]; then
    echo "ERROR: 找不到 xtensa-esp32s3-elf-gcc，请用环境变量 XTGCC 指定路径"
    exit 1
fi
echo "使用编译器: $XTGCC"

cd "$FW_ROOT" || exit 1

if [ ! -f "$BUILD_DIR/compile_commands.json" ]; then
    echo "ERROR: $BUILD_DIR/compile_commands.json 不存在"
    echo "       请先在 $FW_ROOT 执行一次 idf.py build"
    exit 1
fi

if [ -d "$STUB_DIR" ]; then
    echo "桩头目录: $STUB_DIR（含: $(ls -1 "$STUB_DIR" | tr '\n' ' '))"
fi

# 从 compile_commands.json 提取 main.c 的真实编译参数作为模板（同组件）
python - "$BUILD_DIR" <<'PY'
import json, re, sys, shlex, os
bd = sys.argv[1]
db = json.load(open(os.path.join(bd, 'compile_commands.json'), encoding='utf-8'))
ent = [x for x in db if x['file'].replace('\\', '/').endswith('/main/main.c')]
if not ent:
    print('ERROR: compile_commands.json 中找不到 main/main.c'); sys.exit(1)
e = ent[0]
argv = e.get('arguments') or shlex.split(e['command'])
out, skip = [], False
for a in argv:
    if skip:
        skip = False; continue
    if a == '-c':
        continue
    if a == '-o':
        skip = True; continue
    if a.endswith('.c'):
        continue
    if a.startswith('-DPLATFORM'):
        continue          # 平台宏由本脚本显式指定
    out.append(a)
out += ['-DESP_PLATFORM', '-DPLATFORM_ESP32=1']
os.makedirs(os.path.join(bd, '_precheck'), exist_ok=True)
p = os.path.join(bd, '_precheck', 'argv.txt')
with open(p, 'w', encoding='utf-8', newline='\n') as f:
    f.write('\n'.join(out) + '\n')
print('参数个数:', len(out))
PY
[ $? -ne 0 ] && exit 1

mapfile -t ARGV < "$BUILD_DIR/_precheck/argv.txt"
ARGV=("${ARGV[@]:1}")          # 去掉 argv[0]（编译器路径）

# 缺失第三方组件头的判定：错误里出现 "... .h: No such file or directory"
# 且该头不是 IDF 自带组件（这里只对已知第三方头做白名单）。
MISSING_RE='\.h:[0-9]*:? ?(fatal error: )?.*No such file or directory|No such file or directory'

# 编译单文件；若因缺少第三方头失败且有桩头，则带桩头重试一次。
# 输出：编译诊断（可为空字符串=干净）；退出码 0=干净
#
# 关键：必须带 -Werror=implicit-function-declaration。
#   否则「调用了不存在的函数」只会产生 implicit declaration 警告，
#   预检会误判为通过（实测踩过：esp_nimble_init 在 IDF v6.1 不存在，
#   但只有隐式声明警告 → 到链接期才炸）。
WARN_FLAGS=(-Wall -Wextra -Wcomment -Werror=implicit-function-declaration)
compile_one() {
    local f="$1"; shift
    local out
    out=$("$XTGCC" -fsyntax-only -I. "${WARN_FLAGS[@]}" "$@" "$f" 2>&1)
    if [ -n "$out" ] && echo "$out" | grep -qE "$MISSING_RE" && [ -d "$STUB_DIR" ]; then
        local out2
        out2=$("$XTGCC" -fsyntax-only -I. -I"$STUB_DIR" "${WARN_FLAGS[@]}" "$@" "$f" 2>&1)
        # 只有「带桩头后真的干净了」才算 stub 通过；否则仍报原错
        if [ -z "$out2" ]; then
            echo "__STUB_OK__"
            return 0
        fi
        # 若带桩头后错误里仍有 "No such file"（桩头没覆盖到），报原错
        echo "$out2"
        return 1
    fi
    if [ -n "$out" ]; then echo "$out"; return 1; fi
    return 0
}

pass=0; fail=0
echo
echo "=== A. platform/esp32/*.c（host 构建不覆盖，本脚本专测）==="
for f in platform/esp32/*.c; do
    [ -e "$f" ] || continue
    OUT=$(compile_one "$f" "${ARGV[@]}")
    if [ "$OUT" = "__STUB_OK__" ]; then
        echo "[ OK] $f  (stub)"
        pass=$((pass+1))
    elif [ -n "$OUT" ]; then
        echo "[ERR] $f"; echo "$OUT" | head -12; fail=$((fail+1))
    else
        echo "[ OK] $f"; pass=$((pass+1))
    fi
done

echo
echo "=== B. main/main.c（esp32 分支）==="
OUT=$(compile_one "main/main.c" "${ARGV[@]}")
if [ "$OUT" = "__STUB_OK__" ]; then
    echo "[ OK] main/main.c  (stub)"; pass=$((pass+1))
elif [ -n "$OUT" ]; then
    echo "[ERR] main/main.c"; echo "$OUT" | head -12; fail=$((fail+1))
else
    echo "[ OK] main/main.c"; pass=$((pass+1))
fi

echo
echo "=== C. 纯逻辑层（PLATFORM_HOST 语义，host 构建已覆盖，此处复查）==="
for d in ble_hid camera main; do
    for f in "$d"/*.c; do
        [ -e "$f" ] || continue
        [ "$f" = "main/main.c" ] && continue
        OUT=$("$XTGCC" -fsyntax-only -I. -DPLATFORM_HOST=1 "${WARN_FLAGS[@]}" "$f" 2>&1)
        if [ -n "$OUT" ]; then
            echo "[ERR] $f"; echo "$OUT" | head -8; fail=$((fail+1))
        else
            echo "[ OK] $f"; pass=$((pass+1))
        fi
    done
done

echo
echo "========================================"
echo "  clean=$pass  issue=$fail"
echo "========================================"
if [ "$fail" -eq 0 ]; then echo "RESULT: PASS"; else echo "RESULT: FAIL"; fi
exit $fail
