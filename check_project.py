#!/usr/bin/env python3
"""
Antigravity 汉化 — 项目质量自检脚本（纯标准库，零外部依赖）
==========================================================
在修改 dict.json / observer_plus.js / run_observer_plus.py 后执行：
    python3 check_project.py
"""
import json
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
ERRORS = []
WARNINGS = []


def fail(msg):
    ERRORS.append(msg)
    print(f"❌ {msg}")


def warn(msg):
    WARNINGS.append(msg)
    print(f"⚠️  {msg}")


def ok(msg):
    print(f"✅ {msg}")


def check_dict():
    p = ROOT / "dict.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        fail(f"dict.json 解析失败: {e}")
        return

    entries = {k: v for k, v in data.items() if not k.startswith("_")}
    if len(entries) < 1000:
        fail(f"dict.json 词条数异常偏少: {len(entries)}")
    else:
        ok(f"dict.json JSON 合法，有效词条 {len(entries)} 条")

    # 1. 键/值合法性
    bad = [k for k, v in entries.items()
           if not isinstance(v, str) or not v.strip() or k != k.strip() or len(k) < 2 or not re.search(r"[A-Za-z]", k)]
    if bad:
        fail(f"存在 {len(bad)} 条无效词条: {bad[:5]}")

    # 2. 大小写译文冲突检查
    lower_map = {}
    for k, v in entries.items():
        lower_map.setdefault(k.lower(), []).append((k, v))
    conflicts = {kl: vs for kl, vs in lower_map.items() if len(set(v for _, v in vs)) > 1}
    if conflicts:
        fail(f"存在 {len(conflicts)} 组大小写译文冲突: {list(conflicts.items())[:5]}")
    else:
        ok("dict.json 无大小写译文冲突")

    # 3. 确认思考档位保持英文
    efforts = [k for k in ("Minimal", "Low", "Medium", "High", "xHigh", "Extra High", "Max") if k in entries]
    if efforts:
        warn(f"以下思考档位在 dict.json 中被翻译（按约定应保持英文）: {efforts}")

    # 4. 检查冗余 HTML 实体键
    html_entities = [k for k in entries if re.search(r"&(?:amp|lt|gt|quot|nbsp);", k)]
    if html_entities:
        warn(f"dict.json 含 {len(html_entities)} 条 HTML 实体键（DOM textContent 已解码，通常冗余）: {html_entities[:5]}")


def check_js():
    p = ROOT / "observer_plus.js"
    src = p.read_text(encoding="utf-8")
    for token in ("%%DICT%%", "%%DICT_SIG%%", "%%INIT_ZH%%", "%%INIT_HUD%%", "%%MODEL_LIMITS%%", "ENGINE_VERSION"):
        if token not in src:
            fail(f"observer_plus.js 缺少关键标记: {token}")
    dict_occurrences = src.count("%%DICT%%")
    if dict_occurrences != 1:
        fail(f"observer_plus.js 中 %%DICT%% 应仅在尾部出现 1 次，实际出现 {dict_occurrences} 次（会重复注入整份词典）")
    else:
        ok("observer_plus.js 中 %%DICT%% 仅出现 1 次（无重复注入风险）")

    m = re.search(r"ENGINE_VERSION\s*=\s*'([^']+)'", src)
    if not m:
        fail("observer_plus.js 无法提取 ENGINE_VERSION")
    else:
        eng_ver = m.group(1)
        ok(f"observer_plus.js 标记完整（ENGINE_VERSION={eng_ver}）")
        rop_src = (ROOT / "run_observer_plus.py").read_text(encoding="utf-8")
        m_app = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', rop_src)
        if m_app and m_app.group(1) != eng_ver:
            fail(f"版本号不一致：run_observer_plus.py={m_app.group(1)} vs observer_plus.js={eng_ver}")
        elif m_app:
            ok(f"版本号一致（v{eng_ver}）")

    if not src.strip().endswith("(%%DICT%%);"):
        fail("observer_plus.js 结尾不符合 IIFE 契约 '(%%DICT%%);'")

    if not (ROOT / "uninstall.sh").exists():
        fail("缺少 uninstall.sh 卸载脚本")
    else:
        ok("uninstall.sh 就绪")


def check_shell_scripts():
    scripts = sorted(list(ROOT.glob("*.sh")) + list(ROOT.glob("*.command")))
    if not scripts:
        warn("未找到任何 .sh / .command 脚本")
        return
    bash_bin = shutil.which("bash") or "/bin/bash"
    for sp in scripts:
        r = subprocess.run([bash_bin, "-n", str(sp)], capture_output=True, text=True)
        if r.returncode != 0:
            fail(f"{sp.name} Bash 语法错误: {(r.stderr or r.stdout).strip()}")
        else:
            ok(f"{sp.name} Bash 语法检查通过")


def check_bundle_and_js_syntax():
    sys.path.insert(0, str(ROOT))
    import run_observer_plus as rop

    engine_path = ROOT / "observer_plus.js"
    dict_path = ROOT / "dict.json"
    bundle = rop.build_injection_bundle(engine_path, dict_path)
    built = bundle["script"]
    for tok in ("%%DICT%%", "%%DICT_SIG%%", "%%INIT_ZH%%", "%%INIT_HUD%%", "%%MODEL_LIMITS%%"):
        if tok in built:
            fail(f"打包后的注入脚本仍残留未替换占位符: {tok}")
            return

    # 回归验证：即使词典含 */ 字符，也绝不会破坏顶部注释或导致 JS 语法错误
    with tempfile.TemporaryDirectory() as td:
        tmp_dict = Path(td) / "dict.json"
        tmp_dict.write_text(json.dumps({"Test */ glob": "测试 */ 通配符"}, ensure_ascii=False), encoding="utf-8")
        safe_bundle = rop.build_injection_bundle(engine_path, tmp_dict)
        if safe_bundle["script"].count("测试 */ 通配符") != 1:
            fail("词典打包校验失败：测试词条注入次数异常")
            return

        tmp_js = Path(td) / "bundle.js"
        tmp_js.write_text(built, encoding="utf-8")
        tmp_safe_js = Path(td) / "safe_bundle.js"
        tmp_safe_js.write_text(safe_bundle["script"], encoding="utf-8")

        node_bin = shutil.which("node")
        jsc_bin = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"
        if node_bin:
            for label, fp in (("完整注入包", tmp_js), ("含 */ 词典回归包", tmp_safe_js)):
                r = subprocess.run([node_bin, "--check", str(fp)], capture_output=True, text=True)
                if r.returncode != 0:
                    fail(f"{label} JS 语法错误（node --check）: {(r.stderr or r.stdout).strip()}")
                    return
            ok(f"JS 注入包语法校验通过（node --check，体积 {len(built.encode('utf-8')) // 1024} KB）")
        elif Path(jsc_bin).exists():
            for label, fp in (("完整注入包", tmp_js), ("含 */ 词典回归包", tmp_safe_js)):
                r = subprocess.run(
                    [jsc_bin, "-e", "checkSyntax(arguments[0]);", "--", str(fp)],
                    capture_output=True,
                    text=True,
                )
                if r.returncode != 0:
                    fail(f"{label} JS 语法错误（jsc）: {(r.stderr or r.stdout).strip()}")
                    return
            ok(f"JS 注入包语法校验通过（jsc，体积 {len(built.encode('utf-8')) // 1024} KB）")
        else:
            warn("未找到 node 或 jsc，跳过 JS 语法解析检查")


def check_python():
    for name in ("doctor.py", "run_observer_plus.py", "check_project.py"):
        try:
            py_compile.compile(str(ROOT / name), doraise=True)
            ok(f"{name} 语法编译通过")
        except py_compile.PyCompileError as e:
            fail(f"{name} 语法错误: {e}")


def check_incremental_parser():
    sys.path.insert(0, str(ROOT))
    import run_observer_plus as rop

    cid = "11111111-2222-3333-4444-555555555555"
    rop._TRANSCRIPT_CACHE.pop(cid, None)
    with tempfile.TemporaryDirectory() as td:
        tp = Path(td) / "transcript.jsonl"
        line1 = json.dumps({
            "type": "USER_INPUT",
            "source": "USER_EXPLICIT",
            "created_at": "2026-11-27T10:00:00Z",
            "content": "<USER_REQUEST>hello</USER_REQUEST>",
        }) + "\n"
        line2 = json.dumps({
            "source": "MODEL",
            "model": "gemini-4-neon-medium",
            "input_tokens": 1000,
            "cache_read_tokens": 4000,
            "output_tokens": 200,
            "created_at": "2026-11-27T10:00:01Z",
            "status": "DONE",
            "step_index": 1,
        }) + "\n"
        tp.write_text(line1 + line2, encoding="utf-8")
        m1 = rop.parse_transcript_metrics(cid, tp)
        assert m1 and m1["contextTokens"] == 5000 and m1["totalOutputTokens"] == 200, f"m1 异常: {m1}"

        # 追加第 2 轮（含半行测试）
        line3 = json.dumps({
            "source": "MODEL",
            "model": "gemini-4-neon-medium",
            "input_tokens": 500,
            "cache_read_tokens": 5200,
            "output_tokens": 300,
            "created_at": "2026-11-27T10:00:04Z",
            "status": "DONE",
            "step_index": 2,
        }) + "\n"
        with open(tp, "a", encoding="utf-8") as f:
            f.write(line3 + '{"partial": ')
        m2 = rop.parse_transcript_metrics(cid, tp)
        assert m2 and m2["contextTokens"] == 5700 and m2["totalOutputTokens"] == 500 and m2["modelSteps"] == 2, f"m2 异常: {m2}"
    rop._TRANSCRIPT_CACHE.pop(cid, None)
    assert rop.is_antigravity_page({"type": "page", "url": "http://127.0.0.1:12345/c/abc"})
    assert not rop.is_antigravity_page({"type": "page", "url": "devtools://devtools/bundled/inspector.html"})
    assert not rop.is_antigravity_page({"type": "page", "url": "about:blank"})
    ok("transcript.jsonl 字节偏移增量解析 & CDP 目标过滤验证通过")


def main():
    check_dict()
    check_js()
    check_python()
    check_shell_scripts()
    check_bundle_and_js_syntax()
    check_incremental_parser()
    print("-" * 48)
    if ERRORS:
        print(f"自检失败：{len(ERRORS)} 项错误，{len(WARNINGS)} 项警告")
        return 1
    print(f"自检全部通过（{len(WARNINGS)} 项警告）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
