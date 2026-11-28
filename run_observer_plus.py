#!/usr/bin/env python3
"""
Antigravity 汉化 — 后台注入守护进程（run_observer_plus.py）
============================================================
通过本机 127.0.0.1 CDP 调试接口将 observer_plus.js + dict.json 注入
Antigravity 页面，并按需增量推送上下文窗口与 Token 速度遥测数据。

用法:
    python3 run_observer_plus.py                # 前台常驻运行，Ctrl+C 停止
    python3 run_observer_plus.py --once         # 只注入一次后退出
    python3 run_observer_plus.py --no-metrics   # 仅启用界面汉化，关闭小组件数据推送
    python3 run_observer_plus.py --log ~/Library/Logs/agy_zh.log

核心保障:
    · 不修改 Antigravity 任何安装文件
    · 只连 127.0.0.1，零外网访问
    · 持久复用的 CDP WebSocket 连接 + 空闲动态退避轮询
    · 字节偏移增量解析 transcript.jsonl + 有界缓存
"""
import argparse
from datetime import datetime
import hashlib
import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
from doctor import MiniWS, DEVTOOLS_PORT_FILE, DICT_FILE  # noqa: E402

SCRIPT_DIR = Path(__file__).parent.resolve()
APP_VERSION = "0.3.3"
POLL_INTERVAL = 6.0
ACTIVE_POLL_INTERVAL = 0.8
IDLE_POLL_INTERVAL = 2.4
DEEP_IDLE_POLL_INTERVAL = 4.0
PAGES_REFRESH_INTERVAL = 10.0
BRAIN_DIR = Path.home() / ".gemini" / "antigravity" / "brain"
MAX_CACHE_CONVS = 64
MAX_LOG_BYTES = 2 * 1024 * 1024
KEEP_LOG_BYTES = 512 * 1024

# 模型上下文窗口容量上限（单点维护，同步注入到前端 observer_plus.js）
MODEL_CONTEXT_LIMITS = [
    ["claude", 200000],
    ["flash", 256000],
    ["gpt", 272000],
    ["neon", 512000],
]
DEFAULT_CONTEXT_LIMIT = 1000000
CONTEXT_CAP_PRESETS = [0, 1000000, 512000, 272000, 256000, 200000]

_TRANSCRIPT_CACHE = {}
_STEP_END_TS = {}


# ============================================================
# 持久 CDP WebSocket 连接池与注入工具
# ============================================================

class WSPool:
    """按 target_id 复用本机 CDP WebSocket 连接，断线自动重连。"""

    def __init__(self):
        self._conns = {}  # target_id -> (port, MiniWS)

    def _drop(self, target_id):
        item = self._conns.pop(target_id, None)
        if item:
            try:
                item[1].close()
            except Exception:
                pass

    def evaluate(self, port, target_id, expression, timeout=10):
        item = self._conns.get(target_id)
        if item and item[0] == port:
            ws = item[1]
            try:
                ws.sock.settimeout(timeout)
                return ws.evaluate(expression)
            except Exception:
                self._drop(target_id)
        elif item:
            self._drop(target_id)

        ws = MiniWS("127.0.0.1", port, f"/devtools/page/{target_id}", timeout=timeout)
        try:
            res = ws.evaluate(expression)
            self._conns[target_id] = (port, ws)
            return res
        except Exception:
            ws.close()
            raise

    def retain(self, active_target_ids):
        active = set(active_target_ids)
        for tid in list(self._conns.keys()):
            if tid not in active:
                self._drop(tid)

    def clear(self):
        for tid in list(self._conns.keys()):
            self._drop(tid)


_WS_POOL = WSPool()


def get_port():
    if not DEVTOOLS_PORT_FILE.exists():
        return None
    try:
        return int(DEVTOOLS_PORT_FILE.read_text().strip().split("\n")[0])
    except (ValueError, OSError):
        return None


def verify_cdp_server(port):
    """校验本地 127.0.0.1 CDP 端口确实属于 Chromium/Electron/Antigravity 宿主。"""
    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/json/version")
        raw = conn.getresponse().read()
        info = json.loads(raw)
        browser = str(info.get("Browser") or "")
        return any(k in browser for k in ("Chrome", "Electron", "Antigravity"))
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def is_antigravity_page(target):
    """严格筛选 Antigravity 主界面页面，排除 DevTools、扩展与空白占位页。"""
    if target.get("type") != "page":
        return False
    url = (target.get("url") or "").strip()
    if not url:
        return False
    blocked_prefixes = ("data:", "about:", "devtools://", "chrome://", "chrome-extension://", "view-source:")
    if url.startswith(blocked_prefixes):
        return False
    allowed_prefixes = ("http://127.0.0.1", "http://localhost", "https://127.0.0.1", "https://localhost", "app://", "vscode-file://", "file://")
    if url.startswith(allowed_prefixes):
        return True
    # 兜底：若标题包含 Antigravity 也允许注入
    title = target.get("title") or ""
    return "Antigravity" in title


def get_pages(port):
    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/json/list")
        data = json.loads(conn.getresponse().read())
        return [t for t in data if is_antigravity_page(t)]
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def inject(port, target_id, script):
    _WS_POOL.evaluate(port, target_id, script, timeout=15)


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
        r = _WS_POOL.evaluate(port, target_id, expr, timeout=4)
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
    """从注入脚本里读出引擎版本戳（精确锚定常量定义）。"""
    m = re.search(r"ENGINE_VERSION\s*=\s*'([^']+)'", script)
    return m.group(1) if m else None


def force_upgrade(port, target_id):
    """删除页面里的幂等标记，让新引擎热替换接管（无需刷新页面）。"""
    expr = """(() => {
        try { delete window.__agy_zh_injected; } catch (e) { window.__agy_zh_injected = false; }
        return 'ok';
    })()"""
    _WS_POOL.evaluate(port, target_id, expr, timeout=10)


# ============================================================
# 会话日志（transcript.jsonl）解析与实时指标推送
# ============================================================

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


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
    for key, limit in MODEL_CONTEXT_LIMITS:
        if key in m:
            return limit
    return DEFAULT_CONTEXT_LIMIT


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


def _slim_step(rec):
    """提取指标计算所需的最小数值与时间字段，绝不读取或缓存用户聊天正文。"""
    stype = rec.get("type")
    source = rec.get("source")
    slim = {
        "type": stype,
        "source": source,
        "ts": parse_iso_ts(rec.get("created_at")),
        "status": rec.get("status"),
    }
    if source == "MODEL" and ("input_tokens" in rec or "output_tokens" in rec):
        slim["is_model"] = True
        slim["model"] = rec.get("model")
        slim["input_tokens"] = rec.get("input_tokens")
        slim["cache_read_tokens"] = rec.get("cache_read_tokens")
        slim["output_tokens"] = rec.get("output_tokens")
        slim["step_index"] = rec.get("step_index")
        slim["has_tools"] = bool(rec.get("tool_calls")) or ("tool_calls" in (rec.get("truncated_fields") or []))
    return slim


def _prune_transcript_cache():
    if len(_TRANSCRIPT_CACHE) <= MAX_CACHE_CONVS:
        return
    ordered = sorted(_TRANSCRIPT_CACHE.items(), key=lambda kv: kv[1].get("accessed", 0.0))
    evict_ids = set(cid for cid, _ in ordered[: len(ordered) - MAX_CACHE_CONVS])
    for cid in evict_ids:
        _TRANSCRIPT_CACHE.pop(cid, None)
    for key in list(_STEP_END_TS.keys()):
        if key[0] in evict_ids:
            _STEP_END_TS.pop(key, None)


def parse_transcript_metrics(conv_id, transcript_path):
    """按字节偏移增量解析单个会话的 transcript.jsonl，返回上下文占用与 Token 生成速度指标。"""
    try:
        st = transcript_path.stat()
    except OSError:
        return None

    now_ts = time.time()
    cached = _TRANSCRIPT_CACHE.get(conv_id)
    if cached and cached["mtime_ns"] == st.st_mtime_ns and cached["size"] == st.st_size:
        cached["accessed"] = now_ts
        return cached["metrics"]

    start_offset = 0
    if cached and cached.get("inode") == st.st_ino and st.st_size >= cached.get("offset", 0):
        steps = cached.get("steps")
        if steps is None:
            steps = []
        start_offset = cached.get("offset", 0)
    else:
        steps = []

    new_offset = start_offset
    try:
        with open(transcript_path, "rb") as f:
            if start_offset > 0:
                f.seek(start_offset)
            while True:
                line_bytes = f.readline()
                if not line_bytes:
                    break
                # 仅推进到完整换行符位置，避免读取写入中的半行 JSON
                if not line_bytes.endswith(b"\n"):
                    break
                new_offset = f.tell()
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    steps.append(_slim_step(json.loads(line)))
                except ValueError:
                    continue
    except OSError:
        return None

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
            turn_start_ts = step.get("ts")
            turn_end_ts = None
            turn_last_ctx = 0

        if step.get("is_model"):
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

            has_tools = bool(step.get("has_tools"))
            step_idx = step.get("step_index", idx)
            start_ts = step.get("ts")
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
                        if nxt.get("is_model"):
                            break
                        if ntype == "GENERIC":
                            ts_k = nxt.get("ts")
                            if ts_k is not None:
                                last_tool_ts = ts_k
                        elif last_tool_ts is None:
                            ts_k = nxt.get("ts")
                            if ts_k is not None:
                                last_tool_ts = ts_k
                    end_ts = last_tool_ts
                else:
                    nxt = steps[idx + 1]
                    if nxt.get("type") != "USER_INPUT" and nxt.get("source") != "USER_EXPLICIT":
                        end_ts = nxt.get("ts")
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
        last_has_tools = bool(last_rec.get("has_tools"))
        is_final_reply = (last_rec.get("source") == "MODEL" and last_rec.get("status") == "DONE" and not last_has_tools)
        raw_incomplete = not is_final_reply

        last_turn_e2e_tps = None
        if last_turn_duration and last_turn_duration > 0.5 and last_turn_out > 0:
            cand_e2e = round(last_turn_out / last_turn_duration, 1)
            if cand_e2e <= 600:
                last_turn_e2e_tps = cand_e2e

        metrics = {
            "convId": conv_id,
            "convShort": conv_id[:8],
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
            "lastTurnE2eTps": last_turn_e2e_tps,
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
        "inode": st.st_ino,
        "offset": new_offset,
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
        "steps": steps,
        "metrics": metrics,
        "accessed": now_ts,
    }
    _prune_transcript_cache()
    return metrics


def build_metrics_payload(target_conv_id=None):
    """仅精确解析当前路由对应的单个会话指标，零多余目录扫描与历史会话解析。

    关键约束：绝不把其它会话的数据伪装成当前会话的 primary。
    - 当 target_conv_id 为空（新建空白对话页）时，直接零磁盘 I/O 返回空 payload；
    - 当 target_conv_id 有值时，仅增量解析该会话的 transcript.jsonl。
    """
    norm_target = target_conv_id.lower() if (target_conv_id and _UUID_RE.match(target_conv_id)) else None
    if not norm_target or not BRAIN_DIR.exists():
        return json.dumps({
            "targetConvId": norm_target,
            "primary": None,
            "candidates": [],
        }, ensure_ascii=False, separators=(",", ":"))

    tp = BRAIN_DIR / norm_target / ".system_generated" / "logs" / "transcript.jsonl"
    target_metrics = None
    if tp.exists():
        m = parse_transcript_metrics(norm_target, tp)
        if m:
            m_copy = dict(m)
            # 仅当日志最后一条未完结（如正在调工具或等待模型回复）且 120s 内有更新时，兜底判定为工作中
            m_copy["isWorking"] = bool(m_copy.get("rawIncomplete") and (time.time() - m_copy.get("mtimeSec", 0)) < 120)
            target_metrics = m_copy

    return json.dumps({
        "targetConvId": norm_target,
        "primary": target_metrics,
        "candidates": [target_metrics] if target_metrics else [],
    }, ensure_ascii=False, separators=(",", ":"))


def push_metrics(port, target_id, payload_json):
    """通过复用的 CDP WebSocket 将指标推入页面 HUD。"""
    expr = f"if (typeof window.__agy_update_metrics === 'function') window.__agy_update_metrics({payload_json});"
    _WS_POOL.evaluate(port, target_id, expr, timeout=8)


def _file_stamp(p):
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def build_injection_bundle(engine_path, active_dict_path, metrics_only=False, no_metrics=False):
    """构建最终注入 JS 载荷：精准单次替换末尾 (%%DICT%%);，避免注释中的占位符被误替换。"""
    raw_dict = json.loads(active_dict_path.read_text(encoding="utf-8"))
    raw_dict.pop("_meta", None)
    dict_json = json.dumps(raw_dict, ensure_ascii=False, separators=(",", ":"))
    init_zh = "false" if metrics_only else "true"
    init_hud = "false" if no_metrics else "true"
    model_limits_json = json.dumps({
        "rules": MODEL_CONTEXT_LIMITS,
        "defaultLimit": DEFAULT_CONTEXT_LIMIT,
        "presets": CONTEXT_CAP_PRESETS,
    }, ensure_ascii=False, separators=(",", ":"))
    src_text = engine_path.read_text(encoding="utf-8")
    dict_marker = "(%%DICT%%);"
    marker_pos = src_text.rfind(dict_marker)
    if marker_pos == -1:
        raise ValueError(f"{engine_path.name} 缺少末尾 {dict_marker} 占位符")
    sig_seed = (
        f"{dict_json}|zh={init_zh}|hud={init_hud}|limits={model_limits_json}"
        f"|{hashlib.sha256(src_text.encode('utf-8')).hexdigest()[:12]}"
    ).encode("utf-8")
    dict_sig = hashlib.sha256(sig_seed).hexdigest()[:8]
    head = (
        src_text[:marker_pos]
        .replace("%%DICT_SIG%%", dict_sig)
        .replace("%%INIT_ZH%%", init_zh)
        .replace("%%INIT_HUD%%", init_hud)
        .replace("%%MODEL_LIMITS%%", model_limits_json)
    )
    tail = src_text[marker_pos + len(dict_marker):]
    built = f"{head}({dict_json});{tail}"
    return {
        "script": built,
        "entries_count": len(raw_dict),
        "want_ver": engine_version(built),
        "dict_sig": dict_sig,
        "active_dict": active_dict_path,
        "engine_stamp": _file_stamp(engine_path),
        "dict_stamp": _file_stamp(active_dict_path),
    }


def _trim_log_file(path):
    """兼容旧版 stdout 重定向模式：启动时若日志超过 MAX_LOG_BYTES 则截断保留尾部。"""
    try:
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            data = path.read_bytes()[-KEEP_LOG_BYTES:]
            nl = data.find(b"\n")
            if nl != -1:
                data = data[nl + 1:]
            path.write_bytes(data)
    except OSError:
        pass


def _setup_rotating_logger(log_path):
    """配置运行期自动轮转的文件日志器（超过 MAX_LOG_BYTES 自动滚动，保留 1 份旧日志）。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("antigravity_zh")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    handler = RotatingFileHandler(
        str(log_path),
        maxBytes=MAX_LOG_BYTES,
        backupCount=1,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger, handler


# ============================================================

def main():
    # LaunchAgent 把 stdout 重定向到文件时 Python 默认块缓冲（8KB），
    # 会让日志长时间不落盘、看起来像「静默失败」。这里强制行缓冲。
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass

    ap = argparse.ArgumentParser(description=f"Antigravity 汉化 v{APP_VERSION} · 增强引擎守护进程")
    ap.add_argument("--engine", default="observer_plus.js",
                    help="引擎文件（默认 observer_plus.js）")
    ap.add_argument("--dict", default=str(DICT_FILE), help="词典文件")
    ap.add_argument("--source-dict", default=None, help="可选：额外监听的源词典路径（改动时自动热重载）")
    ap.add_argument("--once", action="store_true", help="只注入一次后退出")
    ap.add_argument("--no-metrics", action="store_true", help="仅启用界面汉化，彻底禁用小组件挂载与本地日志读取")
    ap.add_argument("--metrics-only", action="store_true", help="仅启用上下文/Token 速度小组件，保持原始英文界面")
    ap.add_argument("--log", help="将运行日志写入指定文件（自动按大小轮转）")
    args = ap.parse_args()
    if args.no_metrics and args.metrics_only:
        print("❌ --no-metrics 与 --metrics-only 不能同时指定")
        return 1

    file_logger = None
    file_handler = None
    if args.log:
        log_path = Path(args.log).expanduser()
        file_logger, file_handler = _setup_rotating_logger(log_path)
    else:
        # 兼容旧版直接重定向 stdout 到 translate.log 的部署
        _trim_log_file(SCRIPT_DIR / "translate.log")

    def say(msg):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}" if msg else ""
        if sys.stdout and not sys.stdout.closed:
            try:
                print(line)
            except OSError:
                pass
        if file_logger:
            file_logger.info(line)

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

    def load_bundle():
        active_dict = resolve_active_dict_path()
        return build_injection_bundle(
            engine_path,
            active_dict,
            metrics_only=args.metrics_only,
            no_metrics=args.no_metrics,
        )

    try:
        bundle = load_bundle()
    except Exception as e:
        say(f"❌ 加载引擎或词典失败: {e}")
        return 1

    script = bundle["script"]

    mode_label = "仅监测（英文原版）" if args.metrics_only else ("仅汉化（无小组件）" if args.no_metrics else "汉化 + 模型监测")
    say("=" * 56)
    say(f"  Antigravity 汉化 v{APP_VERSION} · 增强引擎守护进程")
    say(f"  运行模式: {mode_label}｜非侵入式 · 纯本地")
    say("=" * 56)
    say(f"📖 词典      : {bundle['active_dict'].name}（{bundle['entries_count']} 条，sig={bundle['dict_sig']}）")
    if source_dict_path and source_dict_path.exists():
        say(f"👀 热重载监听: {source_dict_path}")
    say(f"📄 引擎      : {engine_path.name}（ver={bundle['want_ver'] or '-'}）")
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
    last_active_at = time.time()
    last_wall_ts = time.time()
    cached_pages = []
    last_pages_at = 0.0

    try:
        while True:
            try:
                now_ts = time.time()
                # 检测 Mac 合盖休眠唤醒（墙钟大幅跳跃 > 15s），主动清理半开连接池并刷新页面列表
                if now_ts - last_wall_ts > 15.0:
                    _WS_POOL.clear()
                    last_pages_at = 0.0
                    last_active_at = now_ts
                last_wall_ts = now_ts

                if now_ts - last_reload_check >= 2.0:
                    last_reload_check = now_ts
                    cur_dict_path = resolve_active_dict_path()
                    if (
                        cur_dict_path != bundle["active_dict"]
                        or _file_stamp(cur_dict_path) != bundle["dict_stamp"]
                        or _file_stamp(engine_path) != bundle["engine_stamp"]
                    ):
                        try:
                            new_bundle = load_bundle()
                            bundle = new_bundle
                            script = bundle["script"]
                            want_ver = bundle["want_ver"]
                            want_dict_sig = bundle["dict_sig"]
                            last_active_at = now_ts
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
                        _WS_POOL.clear()
                        cached_pages = []
                        last_pages_at = 0.0
                        last_port = None
                    time.sleep(POLL_INTERVAL)
                    continue

                if port != last_port:
                    if not verify_cdp_server(port):
                        time.sleep(POLL_INTERVAL)
                        continue
                    say(f"🔗 发现 Antigravity（127.0.0.1:{port}）" if last_port is None
                        else f"🔄 Antigravity 已重启（{last_port} → {port}）")
                    doc_ids.clear()
                    last_metrics_sig.clear()
                    _WS_POOL.clear()
                    cached_pages = []
                    last_pages_at = 0.0
                    last_port = port
                    last_active_at = now_ts

                # 节流拉取 /json/list：已有页面缓存且 <10s 时复用，减少无谓 HTTP 短连接
                if not cached_pages or (now_ts - last_pages_at) >= PAGES_REFRESH_INTERVAL:
                    cached_pages = get_pages(port)
                    last_pages_at = now_ts

                valid_tids = []
                any_activity = False
                any_probe_failed = False
                for t in cached_pages:
                    url = t.get("url") or ""
                    if url.startswith("data:") or url == "about:blank":
                        continue
                    tid = t["id"]
                    valid_tids.append(tid)
                    title = (t.get("title") or "未知页面")[:47]

                    info = probe(port, tid)
                    if info is None:
                        any_probe_failed = True
                        continue                      # 页面瞬时不响应，下轮强制刷新列表重试
                    doc = info.get("doc")
                    cur_ver = info.get("version")
                    cur_dict_sig = info.get("dictSig")
                    same_bundle = (cur_ver == want_ver) and (cur_dict_sig == want_dict_sig)

                    # 同一文档 + 引擎在跑 + 版本与词典签名一致 → 仅按需增量推送上下文与速度指标
                    if info.get("engine") and doc_ids.get(tid) == doc and same_bundle:
                        if not args.no_metrics and info.get("hasHud"):
                            try:
                                payload_json = build_metrics_payload(info.get("convId"))
                                if payload_json:
                                    if '"isWorking":true' in payload_json:
                                        any_activity = True
                                    sig = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()[:16]
                                    if last_metrics_sig.get(tid) != sig:
                                        push_metrics(port, tid, payload_json)
                                        last_metrics_sig[tid] = sig
                                        any_activity = True
                            except Exception:
                                any_probe_failed = True
                        continue

                    try:
                        any_activity = True
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
                        any_probe_failed = True
                        say(f"⚠️  注入失败 ({tid[:8]}…): {e}")

                if any_probe_failed:
                    last_pages_at = 0.0
                _WS_POOL.retain(valid_tids)
                if any_activity:
                    last_active_at = time.time()

                if args.once:
                    say("--once 指定：注入完成，退出。")
                    return 0
                idle_for = time.time() - last_active_at
                if idle_for < 12.0:
                    sleep_sec = ACTIVE_POLL_INTERVAL
                elif idle_for < 35.0:
                    sleep_sec = IDLE_POLL_INTERVAL
                else:
                    sleep_sec = DEEP_IDLE_POLL_INTERVAL
                time.sleep(sleep_sec)

            except KeyboardInterrupt:
                say("")
                say("👋 已停止。提示：页面内已注入的脚本会继续运行，")
                say("   如需恢复英文，可在小组件弹窗点击「中」原地切换，或按 Cmd+R 刷新页面。")
                return 0
            except Exception as e:
                say(f"⚠️  错误: {e}")
                time.sleep(POLL_INTERVAL)
    finally:
        _WS_POOL.clear()
        if file_handler:
            try:
                file_handler.close()
            except Exception:
                pass


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main())
