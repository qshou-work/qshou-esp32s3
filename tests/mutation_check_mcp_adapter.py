# -*- coding: utf-8 -*-
"""tests/mutation_check_mcp_adapter.py — 反向验证：证明 MCP 适配器守卫真的会红。

一条恒绿的守卫等于没有守卫。每条变异对应一个真实踩过的坑或明确的失效模式，
跑完必须「全部按预期变红」，出现任何一条 [BAD] 就是漏网。
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST = os.path.join(ROOT, "tests", "test_mcp_adapter.py")
MAP = os.path.join(ROOT, "controller", "mcp_adapter", "schema_map.py")
SRV = os.path.join(ROOT, "controller", "mcp_adapter", "server.py")
DOC = os.path.join(ROOT, "docs", "mcp_agent_integration.md")

# 「类型前缀是否锚定开头」是**组合变异**（去 `^` + 改 .search），
# 单改一处都不造成行为差异，故不放在下面的单点列表里，见 _run_combo。
MUTATIONS = [
    ("类型漏映射 JSON Schema（int 直接透传）",
     MAP,
     '_JSON_TYPE = {"int": "integer", "float": "number",\n'
     '              "string": "string", "bool": "boolean"}',
     '_JSON_TYPE = {"int": "int", "float": "float",\n'
     '              "string": "string", "bool": "bool"}',
     "test_yaml_types_map_to_valid_json_schema_types"),

    ("工具名漏掉 method 后缀（GET/POST 撞名）",
     MAP,
     'return "_".join(segs) + "_" + method.lower()',
     'return "_".join(segs)',
     "test_get_and_post_of_same_path_do_not_collide"),

    # ⚠️ 这条必须**同时**去掉正则里的 `^` 并把 .match() 换成 .search()，只改一处无效：
#    · 只去掉 `^` → .match() 本身就锚定在开头，行为完全不变（第一版犯的错，
#      测出来「漏网」其实是变异压根没造成差异，白排查半天）；
#    · 只换 .search() → 正则里的 `^` 仍起作用，search 依然只匹配开头，无差异。
#    两者同时发生，.search() 才真的会在注释里搜类型词。

    ("未知类型静默降级为 string",
     MAP,
     '                if jt is None:\n'
     '                    raise SchemaError(\n'
     '                        "%s.%s 类型无法识别：%r —— 请把类型前缀写成 "\n'
     '                        "bool/int/float/string（可带 [N]）" % (path, k, desc))',
     '                if jt is None:\n'
     '                    jt, arr = "string", None',
     "test_unknown_type_prefix_raises"),

    ("yaml 缺条目时静默跳过（端点对 Agent 隐形）",
     MAP,
     '        if y is None:\n'
     '            raise SchemaError(\n'
     '                "%s 在 shared/api_schema.yaml 中无 owner=firmware 条目，"',
     '        if y is None:\n'
     '            continue\n'
     '        if False:\n'
     '            raise SchemaError(\n'
     '                "%s 在 shared/api_schema.yaml 中无 owner=firmware 条目，"',
     "test_endpoint_missing_from_yaml_raises"),

    ("yaml 多余参数不报错（Agent 生成设备忽略的参数）",
     MAP,
     '        if y_all - dev_params:\n'
     '            raise SchemaError("%s 的 yaml 参数多于设备自描述：%s（Agent 会生成"',
     '        if False:\n'
     '            raise SchemaError("%s 的 yaml 参数多于设备自描述：%s（Agent 会生成"',
     "test_yaml_has_param_device_does_not_raises"),

    ("base 不一致不报错（所有路径都拼错）",
     MAP,
     '    if device_schema.get("base") != BASE:\n'
     '        raise SchemaError("设备 base=%r 与适配器预期的 %r 不符"',
     '    if False:\n'
     '        raise SchemaError("设备 base=%r 与适配器预期的 %r 不符"',
     "test_base_mismatch_raises"),

    ("所有 POST 字段标 required（逼 LLM 乱填）",
     MAP,
     '            if path_params(path):\n'
     '                schema["required"] = sorted(path_params(path))',
     '            if props:\n'
     '                schema["required"] = sorted(props)',
     "test_path_params_are_the_only_required_fields"),

    ("GET 的 query 混入 body 参数（设备读不到，静默失效）",
     MAP,
     '                "_query": sorted(y_query) if not is_post else [],',
     '                "_query": sorted(props) if not is_post else [],',
     "test_query_params_are_not_required_for_post"),

    ("破坏性工具不再标注（宿主不弹确认）",
     MAP,
     '    "/settings": "写配置后设备立即重启',
     '    "/__removed__": "写配置后设备立即重启',
     "test_destructive_tools_are_flagged"),

    ("风险提示不进描述（LLM 看不到后果）",
     MAP,
     '        if warn:\n            parts.append(warn)',
     '        if False:\n            parts.append(warn)',
     "test_destructive_tools_are_flagged"),

    ("幂等白名单失效（POST 被无条件判幂等）",
     MAP,
     '        "idempotentHint": get_only or path in _IDEMPOTENT_POST,',
     '        "idempotentHint": True,',
     "test_relative_mouse_tools_are_not_idempotent"),

    ("幂等白名单写成死代码（永不被命中）",
     MAP,
     '        "idempotentHint": get_only or path in _IDEMPOTENT_POST,',
     '        "idempotentHint": get_only and path in _IDEMPOTENT_POST,',
     "test_idempotent_whitelist_is_live_not_dead_code"),

    ("固定长度数组不写 minItems/maxItems",
     MAP,
     '                    item = {"type": "array", "items": {"type": jt},\n'
     '                            "minItems": arr, "maxItems": arr}',
     '                    item = {"type": "array", "items": {"type": jt}}',
     "test_array_params_have_fixed_length"),

    ("数值范围不进 schema（LLM 只能靠猜）",
     MAP,
     '                    if rng:\n'
     '                        item["minimum"], item["maximum"] = rng',
     '                    if False:\n'
     '                        item["minimum"], item["maximum"] = rng',
     "test_range_constraints_land_in_schema"),

    ("放开 additionalProperties（LLM 的错键静默通过）",
     MAP,
     '"additionalProperties": False}',
     '"additionalProperties": True}',
     "test_additional_properties_false_blocks_typos"),

    ("只读工具不再标 readOnly",
     MAP,
     '        "readOnlyHint": get_only,',
     '        "readOnlyHint": False,',
     "test_read_only_tools_are_marked"),

    ("日志改写 stdout（污染 JSON-RPC 流）",
     SRV,
     'print("[qshou-mcp] %s" % msg, file=sys.stderr, flush=True)',
     'print("[qshou-mcp] %s" % msg, flush=True)',
     "test_module_level_stdout_writes_are_none"),

    ("顶层 print（握手发不出去）",
     SRV,
     'DEFAULT_HOST = "qshou-0a0c.local"',
     'print("adapter booting")\n\nDEFAULT_HOST = "qshou-0a0c.local"',
     "test_module_level_stdout_writes_are_none"),

    ("文档配置示例塞 PYTHONPATH（OpenClaw 会拒）",
     DOC,
     '        "env": { "QSHOU_HOST": "192.168.110.111" }\n'
     '      }\n    }\n  }\n```\n\n也可以用 CLI',
     '        "env": { "QSHOU_HOST": "192.168.110.111",\n'
     '                "PYTHONPATH": "/x" }\n'
     '      }\n    }\n  }\n```\n\n也可以用 CLI',
     "test_doc_never_suggests_pythonpath"),

    # ⚠️ 必须删掉断言真正依赖的那处内容（dsh-mcp-client 插件名），
    #    改章节标题没用 —— 守卫查的是插件名而非标题。
    ("文档删掉 DeepSeek Harness 的插件接入说明",
     DOC,
     "DSH 官方 MCP 插件通过 `@deepseek-ai/dsh-mcp-client` 接入，配置在 `cordis.yml`：",
     "DSH 的 MCP 接入如下：",
     "test_doc_exists_and_lists_both_hosts"),
]


def _run():
    p = subprocess.run([sys.executable, TEST], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# 组合变异：两处改动单独做都无效，凑一起才造成真实失效。
# 去掉 `^` → .match() 仍锚定开头，无差异；改 .search() → 正则的 `^` 仍生效，
# 也无差异。两者同时发生，.search() 才真的会在注释里搜类型词。
COMBO = [("_TYPE_RE = re.compile(r\"^\\s*(bool|int|float|string)",
          "_TYPE_RE = re.compile(r\"\\s*(bool|int|float|string)"),
         ("    m = _TYPE_RE.match(str(desc))",
          "    m = _TYPE_RE.search(str(desc))")]


def _run_combo():
    src = io.open(MAP, encoding="utf-8").read()
    try:
        mut = src
        for old, new in COMBO:
            assert old in mut, "组合变异锚点未命中：%r" % old
            mut = mut.replace(old, new, 1)
        io.open(MAP, "w", encoding="utf-8", newline="").write(mut)
        return _run()
    finally:
        io.open(MAP, "w", encoding="utf-8", newline="").write(src)


def main():
    src0 = io.open(MAP, encoding="utf-8").read()
    passed = failed = 0
    for desc, path, old, new, expect in MUTATIONS:
        src = io.open(path, encoding="utf-8").read()
        if old not in src:
            print("  [SKIP] 锚点未命中：%s" % desc)
            failed += 1
            continue
        try:
            io.open(path, "w", encoding="utf-8", newline="").write(
                src.replace(old, new, 1))
            rc, out = _run()
            red = rc != 0
            named = expect in out
            if red and named:
                print("  [ OK ] %-52s → 变红且命中 %s" % (desc, expect))
                passed += 1
            else:
                print("  [BAD] %-52s → rc=%d 命中=%s（守卫漏了！）"
                      % (desc, rc, named))
                failed += 1
        finally:
            io.open(path, "w", encoding="utf-8", newline="").write(src)

    rc, out = _run_combo()
    if rc != 0 and "test_only_leading_prefix_is_read" in out:
        print("  [ OK ] %-52s → 变红且命中 test_only_leading_prefix_is_read"
              % "类型前缀改全文搜索（组合变异）")
        passed += 1
    else:
        print("  [BAD] %-52s → rc=%d（守卫漏了！）" % ("类型前缀组合变异", rc))
        failed += 1

    rc, _ = _run()
    print("\n还原后复跑：exit=%d %s" % (rc, "（已恢复全绿）" if rc == 0 else "!! 仍未恢复"))
    print("结果：%d 条按预期变红，%d 条异常" % (passed, failed))
    return 0 if (failed == 0 and rc == 0) else 1


if __name__ == "__main__":
    sys.exit(main())