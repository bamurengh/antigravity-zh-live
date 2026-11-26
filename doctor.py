#!/usr/bin/env python3
"""
Antigravity 汉化 — 健康诊断工具（doctor）
==========================================
纯粹的新增文件：不修改 translate.py / observer.js / dict.json，也不改动
Antigravity 的任何状态。默认只读（唯一的写操作是引擎自身的开关，需显式指定）。

用法:
    python3 doctor.py              # 完整体检（只读）
    python3 doctor.py --json       # 以 JSON 输出，便于脚本消费

它会逐层检查汉化链路的每个环节，并对已确认的故障给出可执行的修复建议：
    [1] 运行环境      python3 路径 / 版本、脚本目录可读性
    [2] Antigravity   App 是否存在、版本、是否在运行
    [3] 调试端口      DevToolsActivePort 是否可读（macOS TCC 常见故障点）
    [4] CDP HTTP      /json/version、/json/list 是否可达
    [5] CDP WebSocket 握手是否成功（只握手、不注入）
    [6] 资源文件      dict.json 结构、observer.js / observer_plus.js 是否就绪
    [7] 注入状态      页面是否已注入、用的哪个引擎、开关状态
    [8] 覆盖率        实时的中文/英文 UI 串统计，量化「翻不全」的程度
"""

import argparse
import base64
import errno
import hashlib
import http.client
import json
import os
import socket
import struct
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
DICT_FILE = SCRIPT_DIR / "dict.json"
OBSERVER_FILE = SCRIPT_DIR / "observer.js"
OBSERVER_PLUS_FILE = SCRIPT_DIR / "observer_plus.js"
TRANSLATE_FILE = SCRIPT_DIR / "translate.py"

APP_SUPPORT = Path.home() / "Library" / "Application Support" / "Antigravity"
DEVTOOLS_PORT_FILE = APP_SUPPORT / "DevToolsActivePort"
APP_CANDIDATES = [
    Path("/Applications/Antigravity.app"),
    Path.home() / "Applications" / "Antigravity.app",
]

CJK_RE = __import__("re").compile(r"[\u4e00-\u9fa5]")
ASCII_WORD_RE = __import__("re").compile(r"[A-Za-z]")


# ============================================================
# 输出helper
# ============================================================

class Report:
    def __init__(self, as_json=False):
        self.items = []
        self.as_json = as_json

    def add(self, level, title, detail="", fix=""):
        self.items.append({"level": level, "title": title, "detail": detail, "fix": fix})
        if not self.as_json:
            icon = {"ok": "✅", "warn": "⚠️ ", "fail": "❌", "info": "• "}[level]
            print(f"{icon} {title}")
            if detail:
                for line in str(detail).split("\n"):
                    print(f"     {line}")
            if fix and level in ("warn", "fail"):
                for line in str(fix).split("\n"):
                    print(f"     ↳ {line}")

    def section(self, name):
        if not self.as_json:
            print(f"\n── {name} ──")

    @property
    def failures(self):
        return [i for i in self.items if i["level"] == "fail"]

    @property
    def warnings(self):
        return [i for i in self.items if i["level"] == "warn"]


# ============================================================
# 极简 WebSocket（比原版更稳健：处理分片、ping、CDP 事件帧）
# ============================================================

class MiniWS:
    def __init__(self, host, port, path, timeout=6):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect((host, port))
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("握手期间连接被关闭")
            buf += chunk
        status = buf.split(b"\r\n")[0]
        if b"101" not in status:
            raise ConnectionError(f"WebSocket 握手失败: {status.decode(errors='replace')}")
        self._buf = buf.split(b"\r\n\r\n", 1)[1]

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise ConnectionError("连接已断开")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, text):
        data = text.encode("utf-8")
        frame = bytearray([0x81])
        n = len(data)
        if n < 126:
            frame.append(0x80 | n)
        elif n < 65536:
            frame.append(0x80 | 126)
            frame.extend(struct.pack(">H", n))
        else:
            frame.append(0x80 | 127)
            frame.extend(struct.pack(">Q", n))
        mask = os.urandom(4)
        frame.extend(mask)
        frame.extend(bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        self.sock.sendall(bytes(frame))

    def recv_text(self):
        """收完整的一条文本消息：跳过 ping/pong，拼接分片，忽略二进制。"""
        payload = b""
        while True:
            b0, b1 = self._read(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else None
            data = self._read(length)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:                      # ping → pong
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if opcode == 0xA:                      # pong
                continue
            if opcode == 0x8:                      # close
                return None
            if opcode in (0x1, 0x2):               # text / binary
                payload = data
            elif opcode == 0x0:                    # continuation
                payload += data
            if fin:
                return payload.decode("utf-8", errors="replace")

    def evaluate(self, expression, timeout_msg="执行超时"):
        self.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": False},
        }))
        # 只等到 id==1 的那条响应；忽略 CDP 事件
        while True:
            raw = self.recv_text()
            if raw is None:
                raise ConnectionError("页面在返回结果前关闭了连接")
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(f"CDP 错误: {msg['error']}")
                res = msg.get("result", {}).get("result", {})
                if res.get("subtype") == "error":
                    raise RuntimeError(res.get("description", "JS 执行错误"))
                return res.get("value")

    def close(self):
        try:
            self.sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ============================================================
# 链路各环节
# ============================================================

def check_environment(rep):
    rep.section("1. 运行环境")
    v = sys.version_info
    if v >= (3, 8):
        rep.add("ok", f"Python {v.major}.{v.minor}.{v.micro}", sys.executable)
    else:
        rep.add("fail", f"Python 版本过低: {v.major}.{v.minor}",
                fix="升级到 Python 3.8+（macOS 自带 3.9 即可）")

    # 脚本目录可读性（TCC 最常挡的就是 Downloads / Desktop / Documents）
    try:
        _ = DICT_FILE.read_bytes()
        rep.add("ok", "脚本目录可读", f"{SCRIPT_DIR}")
    except PermissionError as e:
        rep.add("fail", f"脚本目录不可读：{SCRIPT_DIR}",
                f"errno={e.errno} ({errno.errorcode.get(e.errno, '?')})",
                fix="这是 macOS TCC 拦截（EPERM），典型报错为 "
                    "\"can't open file ...: [Errno 1] Operation not permitted\"。\n"
                    "原因：由「快捷指令 / LaunchAgent / 后台守护」拉起进程时，"
                    "Downloads 目录属于受保护位置。\n"
                    "修复：给发起方（快捷指令 App、终端.app 或对应的 LaunchAgent 可执行文件）"
                    "授予「完全磁盘访问权限」，或将本项目移到 ~/Library/Application Support/ 等非受保护目录。")
    except OSError as e:
        rep.add("warn", "脚本目录读取异常", str(e))


def check_antigravity_app(rep):
    rep.section("2. Antigravity 安装")
    found = None
    for p in APP_CANDIDATES:
        if p.exists():
            found = p
            break
    if not found:
        rep.add("warn", "未在常见位置找到 Antigravity.app",
                detail="已检查: " + ", ".join(str(p) for p in APP_CANDIDATES),
                fix="若装在别处，属正常；只要 App 能运行且暴露调试端口即可。")
        return None

    version = "未知"
    plist = found / "Contents" / "Info.plist"
    try:
        import plistlib
        with open(plist, "rb") as f:
            version = plistlib.load(f).get("CFBundleShortVersionString", "未知")
    except Exception as e:
        rep.add("warn", "无法读取 App 版本", str(e))
    rep.add("ok", f"Antigravity {version}", str(found))
    return version


def read_port_file(rep):
    rep.section("3. 调试端口（DevToolsActivePort）")
    if not DEVTOOLS_PORT_FILE.exists():
        rep.add("warn", "DevToolsActivePort 不存在",
                detail=str(DEVTOOLS_PORT_FILE),
                fix="Antigravity 未运行，或未开启调试端口。先正常启动 Antigravity。")
        return None
    try:
        text = DEVTOOLS_PORT_FILE.read_text().strip()
    except PermissionError as e:
        rep.add("fail", "DevToolsActivePort 不可读（TCC 拦截）",
                detail=f"errno={e.errno} ({errno.errorcode.get(e.errno, '?')})",
                fix="给发起方授予「完全磁盘访问权限」：\n"
                    "系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 加入快捷指令/终端。\n"
                    "授予后需要完全退出并重启该 App 才生效。")
        return None
    except OSError as e:
        rep.add("fail", "读取 DevToolsActivePort 失败", str(e))
        return None

    lines = text.split("\n")
    try:
        port = int(lines[0])
    except (ValueError, IndexError):
        rep.add("fail", "DevToolsActivePort 内容异常", repr(text[:120]))
        return None
    rep.add("ok", f"调试端口 {port}", f"文件内容首行 = {lines[0]}")
    return port


def cdp_http(rep, port):
    rep.section("4. CDP HTTP 接口")
    for path in ("/json/version", "/json/list"):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
            conn.request("GET", path)
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
        except Exception as e:
            rep.add("fail", f"GET {path} 失败", f"{type(e).__name__}: {e}",
                    fix="确认 Antigravity 仍在运行，且端口未被防火墙/其它进程占用。")
            return None
        if path == "/json/version":
            try:
                info = json.loads(raw)
                rep.add("ok", "CDP 可用", f"{info.get('Browser', '?')} / 协议 {info.get('Protocol-Version', '?')}")
            except ValueError:
                rep.add("warn", "/json/version 非 JSON", raw[:120].decode(errors="replace"))
        else:
            try:
                targets = json.loads(raw)
            except ValueError:
                rep.add("fail", "/json/list 非 JSON", raw[:120].decode(errors="replace"))
                return None
            pages = [t for t in targets if t.get("type") == "page"]
            other = [t for t in targets if t.get("type") != "page"]
            if not pages:
                rep.add("warn", "没有可注入的 page 目标",
                        detail=f"全部目标 {len(targets)} 个，非 page 类型 {len(other)} 个",
                        fix="等 Antigravity 界面加载完成后再运行诊断（启动后约 3~5 秒）。")
            else:
                rep.add("ok", f"发现 {len(pages)} 个 page 目标",
                        "\n".join(f"{t.get('type')}: {(t.get('title') or '')[:60]} "
                                  f"[{(t.get('url') or '')[:70]}]" for t in pages))
            return pages
    return None


def cdp_ws(rep, port, pages):
    rep.section("5. CDP WebSocket（只握手，不注入）")
    if not pages:
        rep.add("warn", "跳过：没有 page 目标")
        return None
    # 优先选择主应用窗口（http/https），跳过 data:text/html 占位页
    main_page = next((p for p in pages if (p.get("url") or "").startswith(("http://", "https://"))), pages[0])
    tid = main_page["id"]
    try:
        ws = MiniWS("127.0.0.1", port, f"/devtools/page/{tid}")
    except Exception as e:
        rep.add("fail", "WebSocket 握手失败", f"{type(e).__name__}: {e}",
                fix="这是 translate.py 注入失败的直接原因。重试并确认端口来自 Antigravity。")
        return None
    rep.add("ok", "WebSocket 握手成功", f"/devtools/page/{tid} ({(main_page.get('title') or '')[:50]})")
    return ws


def check_resources(rep, ws):
    rep.section("6. 资源文件")
    if not DICT_FILE.exists():
        rep.add("fail", f"缺少 {DICT_FILE.name}", fix="词典文件缺失，请恢复。")
        return None
    try:
        data = json.loads(DICT_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        rep.add("fail", "dict.json 解析失败", str(e))
        return None
    meta = data.get("_meta")
    entries = {k: v for k, v in data.items() if not k.startswith("_")}
    rep.add("ok", f"词典可用：{len(entries)} 条词条",
            f"_meta = {json.dumps(meta, ensure_ascii=False) if meta else '（无）'}")
    dup = len(data) - len(entries) - (1 if meta else 0)
    if dup:
        rep.add("info", f"另有 {dup} 个下划线开头的保留键")

    for f, label in ((OBSERVER_PLUS_FILE, "翻译引擎"),):
        if not f.exists():
            rep.add("fail", f"{label}未找到：{f.name}")
            continue
        src = f.read_text(encoding="utf-8")
        if "%%DICT%%" not in src:
            rep.add("fail", f"{f.name} 缺少 %%DICT%% 占位符",
                    fix="注入器依赖该占位符写入词典，否则注入内容不含词典。")
        else:
            rep.add("ok", f"{label} {f.name} 就绪",
                    f"{len(src)} 字节｜sha256 {hashlib.sha256(src.encode()).hexdigest()[:12]}")
    return entries


def check_injection(rep, ws, entries):
    rep.section("7. 注入状态")
    if ws is None:
        rep.add("warn", "跳过：没有可用连接")
        return None
    try:
        raw = ws.evaluate("""(() => JSON.stringify({
            injected: !!window.__agy_zh_injected,
            stats: window.__agy_zh_stats || null,
            dictExposed: typeof window.__agy_dict === 'object' && !!window.__agy_dict,
            enabled: window.__agy_zh_enabled === undefined ? null : !!window.__agy_zh_enabled,
            hasUninstall: typeof window.__agy_zh_uninstall === 'function'
        }))()""")
    except Exception as e:
        rep.add("warn", "读取注入状态失败", f"{type(e).__name__}: {e}")
        return None

    st = json.loads(raw)
    if not st["injected"]:
        rep.add("warn", "当前页面尚未注入汉化",
                fix="运行 python3 translate.py 注入；或说明上一条 WebSocket 检查失败才是根因。")
        return st

    engine = (st.get("stats") or {}).get("engine", "observer（原版）")
    rep.add("ok", f"已注入，引擎 = {engine}",
            json.dumps(st.get("stats"), ensure_ascii=False) if st.get("stats") else "（原版引擎不暴露统计）")
    rep.add("info", f"开关 __agy_zh_enabled = {st.get('enabled')}"
                    f"｜可反注 __agy_zh_uninstall = {st.get('hasUninstall')}")
    if engine != "observer_plus":
        rep.add("warn", "当前运行的是原版引擎（缺陷仍在）",
                detail="原版：不监听属性变化、元素级标记过死、仅整串精确匹配。",
                fix="增强引擎 observer_plus.js 已就绪且接口兼容，\n"
                    "经确认后可将其内容替换进 translate.py 的 OBSERVER_FILE 指向（原文件保持不变时可另存副本）。")
    return st


CODE_PREFIXES = ("window.", "//", "/*", "*", ".", "#", "@", "(function", "html,", "div:",
                 "table", "--", "import ", "export ", "const ", "function ", "var ")
PATH_RE = __import__("re").compile(r"^(~|/)[\w./\- ]+$")
FILE_RE = __import__("re").compile(r"^[\w.\-]+\.(js|json|ts|tsx|css|md|py|html|ya?ml|toml)$", __import__("re").I)
TIME_RE = __import__("re").compile(r"^\d+[smhd]$")
DATA_RE = __import__("re").compile(r"^[\w.\-]+/[\w.\-]+$")
JS_RE = __import__("re").compile(r"^(if|for|while|return|switch|try|catch|class|await|async)\b")
SHELL_RE = __import__("re").compile(r"^(python3?|node|npm|pnpm|git|cd|sudo|brew|curl|chmod|ls|cat|echo)\b")
BARE_LITERALS = {"true", "false", "null", "undefined", "NaN"}


def classify_ui(s):
    """把采样到的英文串分成「UI 文案」与「代码/路径/数据」。
    UI 文案才代表汉化覆盖率；把代码和用户数据算进去会严重低估真实覆盖率。"""
    t = s.strip()
    if t in BARE_LITERALS:
        return "code"
    if len(t) > 120 or "\n" in t:
        return "code"
    if t.startswith(CODE_PREFIXES):
        return "code"
    if JS_RE.match(t) or SHELL_RE.match(t):
        return "code"
    if PATH_RE.match(t) or FILE_RE.match(t) or DATA_RE.match(t):
        return "data"
    if TIME_RE.match(t):
        return "data"
    return "ui"


def split_samples(samples):
    ui, other = [], []
    for s in samples:
        (ui if classify_ui(s) == "ui" else other).append(s)
    return ui, other


def check_coverage(rep, ws):
    rep.section("8. 实时翻译覆盖率")
    if ws is None:
        rep.add("warn", "跳过：没有可用连接")
        return None
    try:
        raw = ws.evaluate("""(() => {
          const cn = [], en = [];
          const seenCn = new Set(), seenEn = new Set();
          const ATTRS = ['placeholder','title','aria-label','data-tooltip'];
          let attrTotal = 0, attrCn = 0;
          const attrEn = [];
          for (const a of ATTRS) {
            for (const el of document.querySelectorAll('[' + a + ']')) {
              const v = (el.getAttribute(a) || '').trim();
              if (!v || !/[A-Za-z]/.test(v)) continue;
              attrTotal++;
              if (/[\\u4e00-\\u9fa5]/.test(v)) attrCn++;
              else if (!seenEn.has(v)) { seenEn.add(v); attrEn.push(v.slice(0, 48)); }
            }
          }
          for (const el of document.querySelectorAll('*')) {
            for (const n of el.childNodes) {
              if (n.nodeType !== 3) continue;
              const t = (n.textContent || '').trim();
              if (!t || t.length < 2 || !/[A-Za-z]/.test(t)) continue;
              if (/[\\u4e00-\\u9fa5]/.test(t)) { if (!seenCn.has(t)) { seenCn.add(t); cn.push(t.slice(0,48)); } }
              else if (!seenEn.has(t)) { seenEn.add(t); en.push(t.slice(0,48)); }
            }
          }
          return JSON.stringify({cn: cn.length, en: en.length, enSamples: en.slice(0, 60),
                                 cnSamples: cn.slice(0, 60),
                                 attrTotal: attrTotal, attrCn: attrCn, attrEn: attrEn.slice(0, 15),
                                 totalElements: document.querySelectorAll('*').length});
        })()""")
    except Exception as e:
        rep.add("warn", "覆盖率采样失败", f"{type(e).__name__}: {e}")
        return None

    cov = json.loads(raw)
    ui_en, other = split_samples(cov["enSamples"])
    ui_cn, _ = split_samples(cov.get("cnSamples", []))
    total = cov["cn"] + cov["en"]
    pct = (100.0 * cov["cn"] / total) if total else 0.0
    ui_total = len(ui_en) + len(ui_cn)
    ui_pct = (100.0 * len(ui_cn) / ui_total) if ui_total else 0.0

    level = "ok" if pct >= 70 else ("warn" if pct >= 40 else "fail")
    rep.add(level, f"全部英文串覆盖率 {pct:.1f}%（中文 {cov['cn']} / 英文 {cov['en']}）",
            f"页面元素总数 {cov['totalElements']}｜其中代码/路径/数据 {len(other)} 条不计入 UI")
    if ui_total:
        lv = "ok" if ui_pct >= 70 else ("warn" if ui_pct >= 40 else "fail")
        rep.add(lv, f"纯 UI 文案覆盖率 {ui_pct:.1f}%（中文 {len(ui_cn)} / 仍英文 {len(ui_en)}）")
    if cov["attrTotal"]:
        apct = 100.0 * cov["attrCn"] / cov["attrTotal"]
        rep.add("ok" if apct >= 70 else "warn",
                f"属性（placeholder/title/aria-label/tooltip）覆盖率 {apct:.1f}%"
                f"（中文 {cov['attrCn']} / 英文 {cov['attrTotal'] - cov['attrCn']}）")
    if ui_en:
        rep.add("info", "仍未汉化的 UI 文案样例",
                "\n".join(ui_en[:15]),
                fix="这些就是「精确匹配」翻不到的部分：\n"
                    "· 多词标签（如 Conversation History / Archive conversation）→ 增强引擎的短语切分可覆盖\n"
                    "· 缺词条（如 Install IDE / Load older messages）→ 需在 dict.json 补词条\n"
                    "注：dict.json 属原数据，补充前请先备份，避免覆盖既有 329 条。")
    if other:
        rep.add("info", f"代码 / 路径 / 用户数据 {len(other)} 条（不应翻译）",
                "\n".join(other[:8]))
    if cov["attrEn"]:
        rep.add("info", "仍为英文的属性样例",
                "\n".join(cov["attrEn"][:10]),
                fix="属性覆盖率低是原版引擎的已知缺陷（未监听 attributes + 元素标记过死），"
                    "增强引擎 observer_plus.js 专门修补了这一点。")
    return cov


# ============================================================

def main():
    ap = argparse.ArgumentParser(description="Antigravity 汉化 · 健康诊断")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = ap.parse_args()

    rep = Report(as_json=args.json)
    if not args.json:
        print("=" * 58)
        print("  Antigravity 汉化 · 健康诊断 (doctor)")
        print("  只读检查，不修改任何文件与 App 状态")
        print("=" * 58)

    check_environment(rep)
    check_antigravity_app(rep)
    port = read_port_file(rep)
    pages = cdp_http(rep, port) if port else None
    ws = cdp_ws(rep, port, pages) if port else None
    entries = check_resources(rep, ws)
    check_injection(rep, ws, entries)
    check_coverage(rep, ws)
    if ws:
        ws.close()

    if args.json:
        print(json.dumps({
            "items": rep.items,
            "failures": len(rep.failures),
            "warnings": len(rep.warnings),
        }, ensure_ascii=False, indent=2))
    else:
        print("\n" + "=" * 58)
        if rep.failures:
            print(f"  结论：{len(rep.failures)} 项失败，{len(rep.warnings)} 项警告")
            print("  优先按上面标 ❌ 的「↳」建议处理。")
        elif rep.warnings:
            print(f"  结论：链路可用，{len(rep.warnings)} 项警告（多为可接受的局限）")
        else:
            print("  结论：链路完全健康")
        print("=" * 58)
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
