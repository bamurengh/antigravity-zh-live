#!/usr/bin/env python3
"""
Antigravity 汉化 — 增强引擎启动器（translate.py 的零改动替代入口）
==================================================================
本文件是纯新增文件，translate.py / observer.js / dict.json 保持原样不动。

它与原版 translate.py 共用同一套注入原理（CDP + Runtime.evaluate），差别只有两点：
    1. 注入增强引擎 observer_plus.js（修补了 4 个缺陷）
    2. 支持行内补丁模式：不修改 observer.js，也能把它升级成增强版的内存副本

用法:
    python3 run_observer_plus.py                # 启动汉化（增强引擎），Ctrl+C 停止
    python3 run_observer_plus.py --engine observer.js --patch   # 行内打补丁升级原版引擎
    python3 run_observer_plus.py --log ~/Library/Logs/agy_zh.log
    python3 run_observer_plus.py --once         # 只注入一次就退出（便于 GUI 自动化）

与原版一致的承诺:
    · 不写任何文件（除非显式指定 --log）
    · 只连 127.0.0.1
    · Ctrl+C 干净退出（页面内已注入的脚本随页面刷新消失）
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
from doctor import MiniWS, DEVTOOLS_PORT_FILE, DICT_FILE  # noqa: E402

SCRIPT_DIR = Path(__file__).parent.resolve()
POLL_INTERVAL = 3
ACTIVE_POLL_INTERVAL = 0.8
BRAIN_DIR = Path.home() / ".gemini" / "antigravity" / "brain"

_BRAIN_SCAN_CACHE = {"mtime_ns": 0, "checked_at": 0.0, "dirs": []}
_TRANSCRIPT_CACHE = {}
_STEP_END_TS = {}


# ============================================================
# ============================================================
# 对原版 observer.js 做「内存内补丁」，等价于修掉 3 个缺陷
# ============================================================
# ============================================================

def patch_observer_source(src):
    """把原版 observer.js 在内存里升级成增强版行为。
    只做字符串级替换，源文件保持原样。返回 (补丁后源码, 已应用项列表)。"""
    applied = []

    # ① 监听属性变化（原版缺失 attributes:true）
    old_observe = """  observer.observe(document.body, {
    childList: true,
    subtree: true,
    characterData: true
  });"""
    new_observe = """  observer.observe(document.body, {
    childList: true,
    subtree: true,
    characterData: true,
    attributes: true,
    attributeFilter: ['placeholder', 'title', 'aria-label', 'data-tooltip']
  });"""
    if old_observe in src:
        src = src.replace(old_observe, new_observe)
        applied.append("① 增加 attributes:true + attributeFilter（属性变化后重译）")

    # ② MutationObserver 回调里处理 attributes 变更
    old_cb = """      } else if (mutation.type === 'characterData') {
        processTextNode(mutation.target);
      }"""
    new_cb = """      } else if (mutation.type === 'characterData') {
        processTextNode(mutation.target);
      } else if (mutation.type === 'attributes') {
        var _el = mutation.target;
        delete _el[MARK];              // 元素标记过死会导致属性永不重译，这里放开
        processElementAttrs(_el);
      }"""
    if old_cb in src:
        src = src.replace(old_cb, new_cb)
        applied.append("② attributes 变更回调 + 放开元素级 MARK（可重译）")

    # ③ processElementAttrs 记录我方写入值，避免自触发死循环
    old_attrs_head = """  function processElementAttrs(el) {
    if (el[MARK]) return;"""
    new_attrs_head = """  var __zhAttrWritten = (typeof WeakMap === 'function') ? new WeakMap() : null;
  function processElementAttrs(el) {
    if (el[MARK]) return;
    var __written = __zhAttrWritten ? __zhAttrWritten.get(el) : null;"""
    if old_attrs_head in src:
        src = src.replace(old_attrs_head, new_attrs_head)
        applied.append("③ 引入 WeakMap 记录已写入属性值")

    old_attrs_set = """          el.setAttribute(attrs[i], DICT[trimmed]);"""
    new_attrs_set = """          var __next = DICT[trimmed];
          if (__written && __written[attrs[i]] === __next) continue;   // 我方写入，跳过
          if (__zhAttrWritten) {
            if (!__written) { __written = {}; __zhAttrWritten.set(el, __written); }
            __written[attrs[i]] = __next;
          }
          el.setAttribute(attrs[i], __next);"""
    if old_attrs_set in src:
        src = src.replace(old_attrs_set, new_attrs_set)
        applied.append("③ 写入前比对，防止 attributes 回调自触发死循环")

    # ④ 暴露只读状态，便于 doctor/agyctl 观测（不影响翻译行为）
    old_tail = "  console.log('[汉化] Antigravity 汉化已激活，词典词条数: ' + dictSize);"
    new_tail = """  window.__agy_dict = DICT;
  window.__agy_zh_stats = { engine: 'observer(patched)', dictSize: dictSize,
                            watchingAttributes: true, strict: true };
  console.log('[汉化] Antigravity 汉化已激活（行内补丁版），词典词条数: ' + dictSize);"""
    if old_tail in src:
        src = src.replace(old_tail, new_tail)
        applied.append("④ 暴露 __agy_dict / __agy_zh_stats 供诊断工具观测")

    return src, applied


# ============================================================
# ============================================================
# 连接与注入
# ============================================================
# ============================================================

def get_port():
    if not DEVTOOLS_PORT_FILE.exists():
        return None
    try:
        return int(DEVTOOLS_PORT_FILE.read_text().strip().split("\n")[0])
    except (ValueError, OSError):
        return None


def get_pages(port):
    import http.client
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/json/list")
        data = json.loads(conn.getresponse().read())
        conn.close()
        return [t for t in data if t.get("type") == "page"]
    except Exception:
        return []


def inject(port, target_id, script):
    ws = MiniWS("127.0.0.1", port, f"/devtools/page/{target_id}", timeout=15)
    try:
        ws.evaluate(script)
    finally:
        ws.close()


def probe(port, target_id):
    """探测页面当前的「文档身份」与引擎状态。

    关键教训：**不能用 target id 判断是否已注入**。
    页面刷新（Cmd+R）后 target id 不变，但注入的脚本已随旧文档消失；
    若仍按 target id 记忆「已注入」，新文档会被永远挡在门外 ——
    这正是「一刷新就变全英文」的根因。
    这里用 performance.timeOrigin 作为文档身份，它每次导航都会变。
    同时回传引擎版本，用于「引擎升级后自动替换」。
    """
    expr = """(() => {
        var pathname = String(location.pathname || '');
        var m = pathname.match(/\\/c\\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i);
        var convId = m ? m[1].toLowerCase() : null;
        return JSON.stringify({
            engine: window.__agy_zh_injected ? ((window.__agy_zh_stats || {}).engine || 'observer') : null,
            version: (window.__agy_zh_stats || {}).version || null,
            dictSig: (window.__agy_zh_stats || {}).dictSig || null,
            doc: String(performance.timeOrigin || Date.now()),
            convId: convId,
            hasHud: typeof window.__agy_update_metrics === 'function'
        });
    })()"""
    try:
        ws = MiniWS("127.0.0.1", port, f"/devtools/page/{target_id}", timeout=10)
        try:
            r = ws.evaluate(expr)
        finally:
            ws.close()
    except Exception:
        return None
    if isinstance(r, dict):
        return r
    if isinstance(r, str):
        try:
            return json.loads(r)
        except ValueError:
            return None
    return None


def engine_version(script):
    """从注入脚本里读出引擎版本戳。

    注意：不能用 r"version:\\s*'([^']+)'" —— `version: ENGINE_VERSION` 的行
    在 "version:" 之后不是引号，正则（回溯 + \\s*）会一路滑到后面词典内容里的
    某个 `'...'`，解析出错误的版本号。必须精确锚定常量定义。
    """
    m = __import__("re").search(r"ENGINE_VERSION\s*=\s*'([^']+)'", script)
    return m.group(1) if m else None


def force_upgrade(port, target_id):
    """删除页面里的幂等标记与 observer_plus 记录，让新引擎能重新装上去。
    只清标记，不动页面内容（旧 observer 会与一个空 document.body 的镜像共享状态，
    但新引擎接管后所有节点以新记录为准，不会重复改写文本）。
    """
    expr = """(() => {
        try { delete window.__agy_zh_injected; } catch (e) { window.__agy_zh_injected = false; }
        return 'ok';
    })()"""
    ws = MiniWS("127.0.0.1", port, f"/devtools/page/{target_id}", timeout=10)
    try:
        ws.evaluate(expr)
    finally:
        ws.close()


# ============================================================
# 会话日志（transcript.jsonl）解析与实时指标推送
# ============================================================

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)


def parse_iso_ts(ts_str):
    """解析 ISO8601 时间戳（兼容 Python 3.9，不支持末尾 'Z'）。"""
    if not ts_str or not isinstance(ts_str, str):
        return None
    s = ts_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def infer_context_limit(model_id):
    """根据模型标识推断 Antigravity 中的活跃上下文窗口上限（Tokens）。"""
    m = (model_id or "").lower()
    if "claude" in m:
        return 200000
    if "flash" in m:
        return 256000
    if "gpt" in m:
        return 272000
    if "neon" in m:
        return 512000
    return 1000000


def format_model_name(model_id):
    """将内部模型 ID 格式化为易读名称，如 Gemini 4 Carbon (High)。"""
    if not model_id:
        return "未知模型"
    parts = [p for p in str(model_id).split("-") if p]
    if not parts:
        return str(model_id)
    efforts = {"low", "medium", "high", "xhigh"}
    suffix = ""
    if len(parts) > 1 and parts[-1].lower() in efforts:
        eff = parts.pop().lower()
        suffix = " (xHigh)" if eff == "xhigh" else f" ({eff.capitalize()})"
    words = []
    for p in parts:
        words.append(p.upper() if p.lower() in ("gpt", "pro") else p.capitalize())
    return " ".join(words) + suffix


def extract_user_snippet(content):
    """从 USER_INPUT 正文中提取用户真实输入片段，用于前端匹配当前对话。"""
    if not content or not isinstance(content, str):
        return ""
    m = _USER_REQ_RE.search(content)
    text = m.group(1) if m else content
    text = re.sub(r"\s+", " ", text).strip()
    return text[:36]


def parse_transcript_metrics(conv_id, transcript_path):
    """增量解析单个会话的 transcript.jsonl，返回上下文占用与 Token 生成速度指标。"""
    try:
        st = transcript_path.stat()
    except OSError:
        return None

    cached = _TRANSCRIPT_CACHE.get(conv_id)
    if cached and cached["mtime_ns"] == st.st_mtime_ns and cached["size"] == st.st_size:
        return cached["metrics"]

    steps = []
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    steps.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return None

    first_snippet = ""
    last_snippet = ""
    model_raw = ""
    last_ctx = 0
    last_in = 0
    last_cache = 0
    total_out = 0
    model_steps = 0

    timed_out_sum = 0
    timed_dt_sum = 0.0
    peak_tps = None
    last_tps = None
    last_out_tokens = 0
    last_duration = None
    recent_steps = []

    turn_num = 0
    step_in_turn = 0
    turn_out_sum = 0
    turn_steps = 0
    turn_timed_out_sum = 0
    turn_timed_dt_sum = 0.0
    turn_last_tps = None
    turn_start_ts = None
    turn_end_ts = None
    turn_last_ctx = 0
    turn_end_ctxs = []
    prev_turn_stats = None

    n_steps = len(steps)
    for idx, step in enumerate(steps):
        stype = step.get("type")
        source = step.get("source")
        if stype == "USER_INPUT" or source == "USER_EXPLICIT":
            if turn_steps > 0:
                prev_avg = round(turn_timed_out_sum / turn_timed_dt_sum, 1) if turn_timed_dt_sum > 0 else turn_last_tps
                prev_dur = None
                if turn_start_ts is not None and turn_end_ts is not None and 0 <= (turn_end_ts - turn_start_ts) <= 3600:
                    prev_dur = round(turn_end_ts - turn_start_ts, 1)
                elif turn_timed_dt_sum > 0:
                    prev_dur = round(turn_timed_dt_sum, 1)
                prev_turn_stats = {
                    "avgTps": prev_avg,
                    "outTokens": turn_out_sum,
                    "steps": turn_steps,
                    "durationSec": prev_dur,
                }
                if turn_last_ctx > 0:
                    turn_end_ctxs.append(turn_last_ctx)
            turn_num += 1
            step_in_turn = 0
            turn_out_sum = 0
            turn_steps = 0
            turn_timed_out_sum = 0
            turn_timed_dt_sum = 0.0
            turn_last_tps = None
            turn_start_ts = parse_iso_ts(step.get("created_at"))
            turn_end_ts = None
            turn_last_ctx = 0
            snip = extract_user_snippet(step.get("content"))
            if snip:
                if not first_snippet:
                    first_snippet = snip
                last_snippet = snip

        if source == "MODEL" and ("input_tokens" in step or "output_tokens" in step):
            model_steps += 1
            step_in_turn += 1
            turn_steps += 1
            if step.get("model"):
                model_raw = step["model"]
            in_tok = int(step.get("input_tokens") or 0)
            cache_tok = int(step.get("cache_read_tokens") or 0)
            out_tok = int(step.get("output_tokens") or 0)
            ctx_tok = in_tok + cache_tok

            last_ctx = ctx_tok
            turn_last_ctx = ctx_tok
            last_in = in_tok
            last_cache = cache_tok
            last_out_tokens = out_tok
            total_out += out_tok
            turn_out_sum += out_tok

            has_tools = bool(step.get("tool_calls")) or ("tool_calls" in (step.get("truncated_fields") or []))
            step_idx = step.get("step_index", idx)
            start_ts = parse_iso_ts(step.get("created_at"))
            if turn_start_ts is None and start_ts is not None:
                turn_start_ts = start_ts
            end_ts = None

            if idx + 1 < n_steps:
                if has_tools:
                    # 多工具流式派发时，第 1 个工具在模型生成中途就会启动；
                    # 取紧随其后连续 GENERIC（工具派发）记录中最后一条的 created_at，
                    # 更准确地反映模型吐完所有 tool_calls 的时刻，避免出现几千 tok/s 的假尖峰。
                    last_tool_ts = None
                    for k in range(idx + 1, n_steps):
                        nxt = steps[k]
                        ntype = nxt.get("type")
                        nsrc = nxt.get("source")
                        if ntype == "USER_INPUT" or nsrc == "USER_EXPLICIT":
                            break
                        if nsrc == "MODEL" and ("input_tokens" in nxt or "output_tokens" in nxt):
                            break
                        if ntype == "GENERIC":
                            ts_k = parse_iso_ts(nxt.get("created_at"))
                            if ts_k is not None:
                                last_tool_ts = ts_k
                        elif last_tool_ts is None:
                            ts_k = parse_iso_ts(nxt.get("created_at"))
                            if ts_k is not None:
                                last_tool_ts = ts_k
                    end_ts = last_tool_ts
                else:
                    nxt = steps[idx + 1]
                    if nxt.get("type") != "USER_INPUT" and nxt.get("source") != "USER_EXPLICIT":
                        end_ts = parse_iso_ts(nxt.get("created_at"))
                    else:
                        end_ts = _STEP_END_TS.get((conv_id, step_idx))
            elif step.get("status") == "DONE":
                end_ts = _STEP_END_TS.setdefault((conv_id, step_idx), st.st_mtime)

            if end_ts is not None:
                turn_end_ts = end_ts
            elif start_ts is not None:
                turn_end_ts = start_ts

            dt = None
            tps = None
            if start_ts is not None and end_ts is not None and out_tok > 0:
                raw_dt = end_ts - start_ts
                if 0 <= raw_dt < 0.5:
                    # 秒级时间戳同秒落盘：小输出可用 0.6s 估算，大输出无法可靠测速则跳过
                    if out_tok <= 150:
                        dt = 0.6
                elif 0.5 <= raw_dt <= 600:
                    dt = raw_dt
                if dt is not None:
                    cand_tps = round(out_tok / dt, 1)
                    # 过滤因并发/时间精度造成的非物理尖峰（>600 tok/s）
                    if cand_tps <= 600:
                        tps = cand_tps
                        timed_out_sum += out_tok
                        timed_dt_sum += dt
                        turn_timed_out_sum += out_tok
                        turn_timed_dt_sum += dt
                        last_tps = tps
                        turn_last_tps = tps
                        last_duration = round(dt, 1)
                        if peak_tps is None or tps > peak_tps:
                            peak_tps = tps

            recent_steps.append({
                "step": step_idx,
                "label": f"第{max(1, turn_num)}轮·{step_in_turn}",
                "kind": "工具" if has_tools else "回复",
                "ctx": ctx_tok,
                "cache": cache_tok,
                "out": out_tok,
                "dt": round(dt, 1) if dt is not None else None,
                "tps": tps,
            })

    if model_steps == 0:
        metrics = None
    else:
        avg_tps = round(timed_out_sum / timed_dt_sum, 1) if timed_dt_sum > 0 else None
        cache_hit_rate = round(100.0 * last_cache / last_ctx, 1) if last_ctx > 0 else 0.0

        if turn_steps > 0:
            last_turn_avg_tps = round(turn_timed_out_sum / turn_timed_dt_sum, 1) if turn_timed_dt_sum > 0 else (turn_last_tps or last_tps)
            last_turn_out = turn_out_sum
            last_turn_steps = turn_steps
            current_turn_tps = turn_last_tps
            if turn_start_ts is not None and turn_end_ts is not None and 0 <= (turn_end_ts - turn_start_ts) <= 3600:
                last_turn_duration = round(turn_end_ts - turn_start_ts, 1)
            elif turn_timed_dt_sum > 0:
                last_turn_duration = round(turn_timed_dt_sum, 1)
            else:
                last_turn_duration = None
            if turn_last_ctx > 0:
                turn_end_ctxs.append(turn_last_ctx)
        elif prev_turn_stats:
            last_turn_avg_tps = prev_turn_stats["avgTps"]
            last_turn_out = prev_turn_stats["outTokens"]
            last_turn_steps = prev_turn_stats["steps"]
            last_turn_duration = prev_turn_stats.get("durationSec")
            current_turn_tps = None
        else:
            last_turn_avg_tps = avg_tps or last_tps
            last_turn_out = last_out_tokens
            last_turn_steps = 1
            last_turn_duration = last_duration
            current_turn_tps = None

        avg_ctx_per_turn = None
        if len(turn_end_ctxs) >= 2:
            recent_ctxs = turn_end_ctxs[-6:]
            deltas = [recent_ctxs[i] - recent_ctxs[i - 1] for i in range(1, len(recent_ctxs)) if recent_ctxs[i] > recent_ctxs[i - 1]]
            if deltas:
                avg_ctx_per_turn = int(round(sum(deltas) / len(deltas)))
        elif len(turn_end_ctxs) == 1 and turn_end_ctxs[0] > 0:
            avg_ctx_per_turn = int(turn_end_ctxs[0])

        last_rec = steps[-1] if steps else {}
        last_has_tools = bool(last_rec.get("tool_calls")) or ("tool_calls" in (last_rec.get("truncated_fields") or []))
        is_final_reply = (last_rec.get("source") == "MODEL" and last_rec.get("status") == "DONE" and not last_has_tools)
        raw_incomplete = not is_final_reply

        metrics = {
            "convId": conv_id,
            "convShort": conv_id[:8],
            "isUserConv": bool(first_snippet),
            "firstSnippet": first_snippet,
            "lastSnippet": last_snippet,
            "modelRaw": model_raw,
            "modelName": format_model_name(model_raw),
            "contextLimit": infer_context_limit(model_raw),
            "contextTokens": last_ctx,
            "inputTokens": last_in,
            "cacheTokens": last_cache,
            "cacheHitRate": cache_hit_rate,
            "lastOutputTokens": last_out_tokens,
            "lastDurationSec": last_duration,
            "lastTps": last_tps,
            "avgTps": avg_tps,
            "peakTps": peak_tps,
            "lastTurnAvgTps": last_turn_avg_tps,
            "lastTurnOutputTokens": last_turn_out,
            "lastTurnSteps": last_turn_steps,
            "lastTurnDurationSec": last_turn_duration,
            "avgCtxPerTurn": avg_ctx_per_turn,
            "turnCount": max(1, turn_num),
            "currentTurnTps": current_turn_tps,
            "rawIncomplete": raw_incomplete,
            "mtimeSec": st.st_mtime,
            "totalOutputTokens": total_out,
            "modelSteps": model_steps,
            "recentSteps": recent_steps[-6:],
            "updatedAt": int(st.st_mtime * 1000),
        }

    _TRANSCRIPT_CACHE[conv_id] = {
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
        "metrics": metrics,
    }
    return metrics


def _get_recent_brain_dirs():
    """扫描 brain 目录下的活跃会话，按 transcript 修改时间倒序缓存前 20 个。"""
    if not BRAIN_DIR.exists():
        return []
    now = time.time()
    try:
        dir_st = BRAIN_DIR.stat()
    except OSError:
        return []

    if (
        _BRAIN_SCAN_CACHE["dirs"]
        and _BRAIN_SCAN_CACHE["mtime_ns"] == dir_st.st_mtime_ns
        and (now - _BRAIN_SCAN_CACHE["checked_at"]) < 5.0
    ):
        return _BRAIN_SCAN_CACHE["dirs"]

    items = []
    try:
        with os.scandir(BRAIN_DIR) as it:
            for entry in it:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if not _UUID_RE.match(entry.name):
                    continue
                tp = Path(entry.path) / ".system_generated" / "logs" / "transcript.jsonl"
                try:
                    st = tp.stat()
                    items.append((st.st_mtime, entry.name, tp))
                except OSError:
                    continue
    except OSError:
        return []

    items.sort(key=lambda x: x[0], reverse=True)
    top = [(cid, tp) for _, cid, tp in items[:20]]
    _BRAIN_SCAN_CACHE["mtime_ns"] = dir_st.st_mtime_ns
    _BRAIN_SCAN_CACHE["checked_at"] = now
    _BRAIN_SCAN_CACHE["dirs"] = top
    return top


def build_metrics_payload(target_conv_id=None):
    """汇总当前及近期活跃会话指标，返回 JSON 字符串。

    关键约束：绝不把其它会话的数据伪装成当前会话的 primary。
    - 当 target_conv_id 为空（新建空白对话页）时，primary 严格为 None；
    - 当 target_conv_id 有值但磁盘尚无对应模型日志时，primary 也严格为 None。
    """
    if not BRAIN_DIR.exists():
        return json.dumps({
            "targetConvId": target_conv_id,
            "primary": None,
            "candidates": [],
        }, ensure_ascii=False, separators=(",", ":"))

    dirs = list(_get_recent_brain_dirs())
    norm_target = target_conv_id.lower() if (target_conv_id and _UUID_RE.match(target_conv_id)) else None
    if norm_target:
        if not any(cid.lower() == norm_target for cid, _ in dirs):
            tp = BRAIN_DIR / norm_target / ".system_generated" / "logs" / "transcript.jsonl"
            if tp.exists():
                dirs.insert(0, (norm_target, tp))

    now = time.time()
    candidates = []
    target_metrics = None
    for cid, tp in dirs[:12]:
        m = parse_transcript_metrics(cid, tp)
        if not m:
            continue
        m_copy = dict(m)
        # 仅当日志最后一条未完结（如正在调工具或等待模型回复）且 120s 内有更新时，兜底判定为工作中
        m_copy["isWorking"] = bool(m_copy.get("rawIncomplete") and (now - m_copy.get("mtimeSec", 0)) < 120)
        candidates.append(m_copy)
        if norm_target and cid.lower() == norm_target:
            target_metrics = m_copy

    candidates.sort(key=lambda m: m["updatedAt"], reverse=True)
    return json.dumps({
        "targetConvId": norm_target,
        "primary": target_metrics,
        "candidates": candidates[:8],
    }, ensure_ascii=False, separators=(",", ":"))


def push_metrics(port, target_id, payload_json):
    """通过 CDP 将指标推入页面 HUD。"""
    expr = f"if (typeof window.__agy_update_metrics === 'function') window.__agy_update_metrics({payload_json});"
    ws = MiniWS("127.0.0.1", port, f"/devtools/page/{target_id}", timeout=8)
    try:
        ws.evaluate(expr)
    finally:
        ws.close()


# ============================================================

def main():
    # LaunchAgent 把 stdout 重定向到文件时 Python 默认块缓冲（8KB），
    # 会让日志长时间不落盘、看起来像「静默失败」。这里强制行缓冲。
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Antigravity 汉化 · 增强引擎启动器")
    ap.add_argument("--engine", default="observer_plus.js",
                    help="引擎文件（默认 observer_plus.js；可指定 observer.js）")
    ap.add_argument("--patch", action="store_true",
                    help="对指定引擎做行内补丁（用于在不改文件的前提下升级原版 observer.js）")
    ap.add_argument("--dict", default=str(DICT_FILE), help="词典文件")
    ap.add_argument("--source-dict", default=None, help="可选：额外监听的源词典路径（改动时自动热重载）")
    ap.add_argument("--once", action="store_true", help="只注入一次后退出")
    ap.add_argument("--no-metrics", action="store_true", help="禁用上下文窗口与 Token 速度悬浮条数据推送")
    ap.add_argument("--log", help="把输出同时追加到该文件")
    args = ap.parse_args()

    out = sys.stdout
    logf = None
    if args.log:
        logf = open(Path(args.log).expanduser(), "a", encoding="utf-8", buffering=1)

    def say(msg):
        print(msg)
        if logf:
            logf.write(msg + "\n")

    engine_path = Path(args.engine)
    if not engine_path.is_absolute():
        engine_path = SCRIPT_DIR / engine_path
    dict_path = Path(args.dict)
    if not dict_path.is_absolute():
        dict_path = SCRIPT_DIR / dict_path

    source_dict_path = Path(args.source_dict).expanduser() if args.source_dict else None

    if not engine_path.exists():
        say(f"❌ 引擎文件不存在: {engine_path}")
        return 1
    if not dict_path.exists():
        say(f"❌ 词典文件不存在: {dict_path}")
        return 1

    def resolve_active_dict_path():
        if source_dict_path:
            try:
                if source_dict_path.exists() and os.access(source_dict_path, os.R_OK):
                    with open(source_dict_path, "rb") as _f:
                        _f.read(1)
                    return source_dict_path
            except OSError:
                pass
        return dict_path

    def file_stamp(p):
        try:
            st = p.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def load_bundle():
        active_dict = resolve_active_dict_path()
        raw_dict = json.loads(active_dict.read_text(encoding="utf-8"))
        raw_dict.pop("_meta", None)
        dict_json = json.dumps(raw_dict, ensure_ascii=False, separators=(",", ":"))
        dict_sig = hashlib.sha256(dict_json.encode("utf-8")).hexdigest()[:8]

        src_text = engine_path.read_text(encoding="utf-8")
        applied_patches = []
        if args.patch:
            src_text, applied_patches = patch_observer_source(src_text)
        if "%%DICT%%" not in src_text:
            raise ValueError(f"{engine_path.name} 缺少 %%DICT%% 占位符")
        built = src_text.replace("%%DICT_SIG%%", dict_sig).replace("%%DICT%%", dict_json)
        return {
            "script": built,
            "entries_count": len(raw_dict),
            "patched": applied_patches,
            "want_ver": engine_version(built),
            "dict_sig": dict_sig,
            "active_dict": active_dict,
            "engine_stamp": file_stamp(engine_path),
            "dict_stamp": file_stamp(active_dict),
        }

    try:
        bundle = load_bundle()
    except Exception as e:
        say(f"❌ 加载引擎或词典失败: {e}")
        return 1

    script = bundle["script"]
    patched = bundle["patched"]

    say("=" * 56)
    say("  Antigravity 汉化 · 增强引擎启动器")
    say("  非侵入式 · 纯本地 · 不修改任何原文件")
    say("=" * 56)
    say(f"📖 词典      : {bundle['active_dict'].name}（{bundle['entries_count']} 条，sig={bundle['dict_sig']}）")
    if source_dict_path and source_dict_path.exists():
        say(f"👀 热重载监听: {source_dict_path}")
    say(f"📄 引擎      : {engine_path.name}"
        + ("（已应用行内补丁）" if args.patch else "（原样使用）"))
    if patched:
        for p in patched:
            say(f"   · {p}")
    say(f"🔍 端口文件  : {DEVTOOLS_PORT_FILE}")
    say("按 Ctrl+C 停止")
    say("-" * 56)

    # 记忆「每个 target 的文档身份 + 引擎版本」，而不是简单记住 target id。
    # 刷新后 target id 不变但文档身份会变 → 才能正确重新注入。
    doc_ids = {}
    last_metrics_sig = {}
    want_ver = bundle["want_ver"]
    want_dict_sig = bundle["dict_sig"]
    last_port = None
    last_reload_check = time.time()

    while True:
        try:
            now_ts = time.time()
            if now_ts - last_reload_check >= 2.0:
                last_reload_check = now_ts
                cur_dict_path = resolve_active_dict_path()
                if (
                    cur_dict_path != bundle["active_dict"]
                    or file_stamp(cur_dict_path) != bundle["dict_stamp"]
                    or file_stamp(engine_path) != bundle["engine_stamp"]
                ):
                    try:
                        new_bundle = load_bundle()
                        bundle = new_bundle
                        script = bundle["script"]
                        want_ver = bundle["want_ver"]
                        want_dict_sig = bundle["dict_sig"]
                        say(f"♻️  检测到词典/引擎更新，热重载就绪（{bundle['entries_count']} 条，sig={want_dict_sig}，ver={want_ver}）")
                    except Exception as e:
                        say(f"⚠️  词典/引擎热重载解析失败（保留旧版运行）: {e}")

            port = get_port()
            if not port:
                if last_port is not None:
                    say("⏸  Antigravity 似乎已关闭，等待重新启动…")
                    say("   提示：2.34 起须用「启动Antigravity（汉化）.command」启动"
                        "（带 --remote-debugging-port），否则不会生成端口文件")
                    doc_ids.clear()
                    last_metrics_sig.clear()
                    last_port = None
                time.sleep(POLL_INTERVAL)
                continue

            if port != last_port:
                say(f"🔗 发现 Antigravity（端口 {port}）" if last_port is None
                    else f"🔄 Antigravity 已重启（{last_port} → {port}）")
                doc_ids.clear()
                last_metrics_sig.clear()
                last_port = port

            for t in get_pages(port):
                url = t.get("url") or ""
                if url.startswith("data:") or url == "about:blank":
                    continue
                tid = t["id"]
                title = (t.get("title") or "未知页面")[:47]

                info = probe(port, tid)
                if info is None:
                    continue                      # 页面瞬时不响应，下轮再试
                doc = info.get("doc")
                cur_ver = info.get("version")
                cur_dict_sig = info.get("dictSig")
                same_bundle = (cur_ver == want_ver) and (not cur_dict_sig or cur_dict_sig == want_dict_sig)

                # 同一文档 + 引擎在跑 + 版本与词典签名一致 → 仅按需增量推送上下文与速度指标
                if info.get("engine") and doc_ids.get(tid) == doc and same_bundle:
                    if not args.no_metrics and info.get("hasHud"):
                        try:
                            payload_json = build_metrics_payload(info.get("convId"))
                            if payload_json:
                                sig = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()[:16]
                                if last_metrics_sig.get(tid) != sig:
                                    push_metrics(port, tid, payload_json)
                                    last_metrics_sig[tid] = sig
                        except Exception:
                            pass
                    continue

                try:
                    if info.get("engine") and not same_bundle:
                        # 引擎版本或词典签名变化 → 清幂等标记后热替换（无需刷新页面）
                        say(f"⬆️  热升级 {cur_ver or '(旧版)'}/{cur_dict_sig or '-'} → {want_ver}/{want_dict_sig}: {title}")
                        force_upgrade(port, tid)
                    inject(port, tid, script)
                    doc_ids[tid] = doc
                    last_metrics_sig.pop(tid, None)
                    if info.get("engine"):
                        say(f"🔁 文档已刷新/热重载，重新注入: {title}")
                    else:
                        say(f"✅ 已注入增强汉化: {title}")
                    if not args.no_metrics:
                        try:
                            payload_json = build_metrics_payload(info.get("convId"))
                            if payload_json:
                                push_metrics(port, tid, payload_json)
                                last_metrics_sig[tid] = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()[:16]
                        except Exception:
                            pass
                except Exception as e:
                    say(f"⚠️  注入失败 ({tid[:8]}…): {e}")

            if args.once:
                say("--once 指定：注入完成，退出。")
                return 0
            time.sleep(ACTIVE_POLL_INTERVAL)

        except KeyboardInterrupt:
            say("")
            say("👋 已停止。注意：页面内已注入的脚本会继续运行，")
            say("   要完全恢复英文请刷新页面（Cmd+R）——原版 README 说 Ctrl+C 即恢复，实际不成立。")
            return 0
        except Exception as e:
            say(f"⚠️  错误: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main())
