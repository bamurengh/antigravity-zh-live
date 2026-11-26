/**
 * Antigravity 汉化 — DOM 翻译引擎（增强版 / 非侵入修补件）
 * =========================================================
 * 本文件是 observer.js 的「可替换增强版」，按与原版完全相同的方式被注入：
 *     translate.py 读取本文件 → 把 %%DICT%% 换成 dict.json 的内容 → Runtime.evaluate
 * 它不修改、不依赖任何原文件，也不改动 dict.json 的任何数据。
 *
 * 相对原版 observer.js 修补的 4 个缺陷：
 *   ① 动态文案不支持      原版只做「整串精确匹配」，任何带数字/变量的文案都翻不了
 *                         （如 "Worked for 42s"）。本版增加「按词切分的贪心最长匹配」，
 *                         且要求整串被完全消费，绝不产出中英混杂的结果。
 *   ② 属性变化不监听      原版 observe() 未声明 attributes，React 动态改
 *                         placeholder/aria-label 后永不重译。本版加 attributeFilter。
 *   ③ 元素级标记过粗      原版用 el[MARK] 一票否决整个元素，属性再也无法重译。
 *                         本版用 WeakMap 记录「英文原值」，既能识别我方写入、
 *                         防止自触发死循环，又能在应用改回英文时重新翻译。
 *   ④ 无法停用/无法观测   原版没有开关也不暴露状态。本版支持
 *                         window.__agy_zh_enabled 开关，并暴露只读的
 *                         window.__agy_dict / __agy_zh_stats 供外部审计。
 *
 * 与原版的兼容契约（严格保持不变）：
 *   · 注入占位符  %%DICT%%
 *   · 幂等标记    window.__agy_zh_injected
 *   · 跳过        script/style/code/pre/textarea/svg、language-* 代码块、路径、URL
 */

(function(DICT) {
  'use strict';

  // ====== 幂等（与原版一致的契约）======
  if (window.__agy_zh_injected) {
    var prev = window.__agy_zh_stats;
    console.log('[汉化] 页面已注入过引擎，跳过。当前引擎: ' +
                (prev && prev.engine ? prev.engine : '未知'));
    return;
  }
  // 若页面里已有一个「旧版引擎实例」在跑，先把它的回调/监听停掉。
  // 否则热升级后两个实例并存：旧实例的闭包不认新标记，会继续按老规则改写文本，
  // 导致「切回英文」时部分内容被它重新翻回中文。
  try { if (typeof window.__agy_zh_uninstall === 'function') window.__agy_zh_uninstall(); } catch (e) {}
  window.__agy_zh_injected = true;

  if (!DICT || typeof DICT !== 'object') {
    console.warn('[汉化] 词典为空或格式错误，引擎未启动');
    window.__agy_zh_injected = false;
    return;
  }

  // ====== 配置 ======
  var CFG = {
    // false = 严格：整串必须被完全翻译，否则整串放弃（默认，绝不中英混杂）
    // true  = 宽松：允许残留少量英文（如 "Display Options" → "Display 选项"）
    allowPartialPhrases: false,
    budgetMs: 12,         // 单批处理时间预算（<16ms，兼顾流畅度与大列表瞬时翻译）
    maxScanNodes: 20000   // 单次遍历节点上限
  };

  // ====== 建词典（_meta 再兜一层；支持可选的 _config 覆盖）======
  var dict = {};
  var dictLower = {};
  var dictKeys = [];
  (function buildDict() {
    for (var k in DICT) {
      if (!Object.prototype.hasOwnProperty.call(DICT, k)) continue;
      if (k === '_meta') continue;
      if (k === '_config') {
        if (DICT[k] && typeof DICT[k] === 'object') {
          for (var c in DICT[k]) {
            if (Object.prototype.hasOwnProperty.call(CFG, c)) CFG[c] = DICT[k][c];
          }
        }
        continue;
      }
      var v = DICT[k];
      if (typeof k !== 'string' || typeof v !== 'string') continue;
      var kk = k.trim();
      if (kk.length < 2 || !/[a-zA-Z]/.test(kk)) continue;
      dict[kk] = v;
      if (kk.length >= 4 || kk.indexOf(' ') !== -1) {
        var kl = kk.toLowerCase();
        if (!Object.prototype.hasOwnProperty.call(dictLower, kl)) dictLower[kl] = v;
      }
    }
    dictKeys = Object.keys(dict);
  })();

  // 只读暴露，供 doctor.py / agyctl.py 审计覆盖率（不参与翻译决策）
  try { window.__agy_dict = dict; } catch (e) {}

  // ====== 常量 ======
  var ENGINE_VERSION = '2026.11.26.18';  // 引擎版本：语义变化时递增，供注入器判断是否需要热替换
  var DICT_SIG = '%%DICT_SIG%%';         // 词典签名：词典热重载时变更，供注入器判断热替换
  var MARK = typeof Symbol === 'function' ? Symbol('zh') : '__zh_mark__';
  var TR_ATTRS = ['placeholder', 'title', 'aria-label', 'data-tooltip'];
  // 极短高歧义词：只有作为元素内唯一文本（不含相邻拆句节点、不带前后空格拼接）时才允许翻译，
  // 避免 React 拆句（如 "No " + "automations" + " yet"）被误翻成 "否 自动化任务 yet"。
  var AMBIGUOUS_SHORT = { No: 1, Yes: 1, On: 1, Off: 1, In: 1, Or: 1, To: 1 };
  var SKIP_TAGS = {
    SCRIPT: 1, STYLE: 1, CODE: 1, PRE: 1, TEXTAREA: 1, SVG: 1,
    INPUT: 1, SELECT: 1, OPTION: 1, KBD: 1, SAMP: 1
  };
  var HAS_WEAKMAP = typeof WeakMap === 'function';
  var HAS_WEAKSET = typeof WeakSet === 'function';

  // ====== 文本保护：这些内容绝不动 ======
  function looksLikeCode(t) {
    if (/[{};=<>]/.test(t) && t.length > 20) return true;
    if (/^(\/\/|\/\*|\*|\.|#|@)/.test(t)) return true;
    if (/^(function|var|let|const|import|export|class|def)\s/.test(t)) return true;
    if (/^[\w.-]+\.(js|json|ts|tsx|css|md|py|html|ya?ml|toml)$/i.test(t)) return true;
    if (/^[a-z0-9_-]+\/[a-z0-9_.-]+$/i.test(t)) return true;
    if (/\b[a-z]+\([^)]*\)\s*\{/.test(t)) return true;
    return false;
  }
  function looksLikeData(t) {
    if (/^https?:\/\//.test(t)) return true;
    if (/^[\/~]/.test(t)) return true;
    if (/^\d+(\.\d+)*$/.test(t)) return true;
    if (/^[\w.+-]+@[\w.-]+$/.test(t)) return true;
    if (/^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(t)) return true;
    return false;
  }
  function isSkippableParent(el) {
    if (!el) return true;
    if (el.id === '__agy_metrics_host') return true;
    if (SKIP_TAGS[el.tagName]) return true;
    var cls = el.className;
    if (typeof cls === 'string' && cls.indexOf('language-') !== -1) return true;
    if (el.isContentEditable) return true;
    return false;
  }

  // ====== 短语切分翻译（本版核心增强）======
  // 用捕获组切分：奇数下标是英文片段，偶数下标是「它前面的分隔符」，
  // 这样分隔符（空格/标点）始终跟着它所属的片段，不会变成独立片段导致重复空格。
  var SPLIT_RE = /([A-Za-z][A-Za-z'’.\-]*)/;

  /**
   * 在一段连续英文里做贪心最长匹配。
   * 返回 { text: 已翻译片段, tail: 未能识别的残留原文 }
   * 关键：tail 必须原样返回，否则严格模式无法发现"中英混杂"。
   */
  function matchRun(text) {
    var produced = '', tail = '', i = 0, guard = 0;
    while (i < text.length && guard++ < 128) {
      var best = null;
      for (var j = 0; j < dictKeys.length; j++) {
        var k = dictKeys[j];
        if (k.length > text.length - i) continue;
        if (text.substr(i, k.length) !== k) continue;
        var after = text.charAt(i + k.length);
        if (after && /[A-Za-z0-9]/.test(after)) continue;   // 必须在词边界结束
        if (!best || k.length > best.length) best = k;
      }
      if (best) { produced += dict[best]; i += best.length; }
      else {
        // 未命中：原样收进 tail，保证「原文 = produced + tail」
        tail += text.charAt(i);
        i++;
      }
    }
    return { text: produced, tail: tail };
  }

  var SEG_CJK = /[\u4e00-\u9fa5\d]/;

  /**
   * 整串翻译：按「英文片段 / 前导分隔符」配对处理。
   *
   * 拼接规则（决定空格保留与否）：
   *   · 两侧都是我方译文 → 直接粘连（"对话"+"历史" = "对话历史"）
   *   · 未命中的原始英文夹在中间 → 两侧补空格（"设置 in 设置"）
   *   · 原文里译文前面的标点 / 空格 → 原样保留（"加载, 更早的"）
   *   · 其余情况紧凑拼接，避免出现重复空格
   *
   * 严格模式（默认）：只要还有英文残留就整串放弃，绝不产出中英混杂。
   */
  function translatePhrase(src) {
    var parts = src.split(SPLIT_RE);
    var out = '', leftover = '', changed = false;
    var seg = [];   // {t: 片段文本, tr: 是否我方译文, sep: 是否带原文前导分隔符}

    for (var i = 1; i < parts.length; i += 2) {
      var p = parts[i];                     // 英文片段（奇数下标）
      var lead = parts[i - 1] || '';        // 它前面的分隔符（偶数下标）
      var r = matchRun(p);
      changed = true;
      if (r.text) seg.push({ t: r.text, tr: true, sep: lead });
      if (r.tail) {
        seg.push({ t: r.tail, tr: false, sep: r.text ? '' : lead });
        leftover += r.tail.replace(/\d+/g, '');   // 纯数字是运行时动态值，不算混杂
      }
    }
    if (!changed || !seg.length) return null;
    if (!CFG.allowPartialPhrases && /[A-Za-z0-9]/.test(leftover)) return null;

    // 关键：原文里的空格（sep）不能无条件保留。
    // 英文靠空格分词，但中文不需要 —— "Conversation History" 的两段各自译为
    // 「对话」「历史」，中间那个原文空格必须丢掉，否则渲染成「对话 历史」。
    // 规则：相邻两侧都是我方译文，且交界处是中文/数字，就去掉空格。
    for (var k = 0; k < seg.length; k++) {
      var s = seg[k];
      if (k > 0) {
        var prev = seg[k - 1];
        var prevCh = out.slice(-1);
        var nextCh = s.sep ? s.sep.charAt(0) : s.t.charAt(0);
        var collapse = prev.tr && s.tr && /[\u4e00-\u9fa5\d]/.test(prevCh) && /[\u4e00-\u9fa5\d]/.test(nextCh);
        if (collapse) {
          out = out.replace(/\s+$/, '');
          s.sep = s.sep.replace(/^\s+/, '');
        } else if (s.sep) {
          out += s.sep;                       // 原文的分隔符照旧保留
        }
      } else if (s.sep) {
        out += s.sep;
      }
      out += s.t;
    }
    // 中文之间不留空格（兜底）
    out = out.replace(/([\u4e00-\u9fa5])\s+(?=[\u4e00-\u9fa5])/g, '$1');
    return out.trim();
  }

  // 将英文时长（如 "4 hours, 45 minutes" / "6d 23h"）转为中文
  function translateDuration(s) {
    return String(s)
      .replace(/\b(\d+)\s*days?\b/g, '$1 天')
      .replace(/\b(\d+)\s*hours?\b/g, '$1 小时')
      .replace(/\b(\d+)\s*minutes?\b/g, '$1 分钟')
      .replace(/\b(\d+)\s*mins?\b/g, '$1 分钟')
      .replace(/\b(\d+)\s*seconds?\b/g, '$1 秒')
      .replace(/\b(\d+)\s*secs?\b/g, '$1 秒')
      .replace(/\b(\d+)d\b/g, '$1 天')
      .replace(/\b(\d+)h\b/g, '$1 小时')
      .replace(/\b(\d+)m\b/g, '$1 分钟')
      .replace(/\b(\d+)s\b/g, '$1 秒')
      .replace(/,\s*/g, ' ');
  }

  function lookupWord(w) {
    if (Object.prototype.hasOwnProperty.call(dict, w)) return dict[w];
    var wl = w.toLowerCase();
    if (Object.prototype.hasOwnProperty.call(dictLower, wl)) return dictLower[wl];
    return w;
  }

  // ====== 正则模式匹配：处理含动态数字/时间的 UI 文本 ======
  var PATTERNS = [
    // 额度刷新时间（含英文时长中文化）
    [/^You have used some of your weekly limit, it will fully refresh in (.+)\.$/, function(m) { return '你已使用了部分每周额度，将在 ' + translateDuration(m[1]) + ' 后完全刷新。'; }],
    [/^You have used some of your 5-hour limit, it will fully refresh in (.+)\.$/, function(m) { return '你已使用了部分五小时额度，将在 ' + translateDuration(m[1]) + ' 后完全刷新。'; }],
    // 文件变更数
    [/^(\d+) files? changed$/, function(m) { return '更改了 ' + m[1] + ' 个文件'; }],
    // 思考/工作时间（精确单位优先于泛化格式）
    [/^Thought for (\d+)s$/, function(m) { return '思考了 ' + m[1] + ' 秒'; }],
    [/^Worked for (\d+)s$/, function(m) { return '工作了 ' + m[1] + ' 秒'; }],
    [/^Worked for (\d+)m$/, function(m) { return '工作了 ' + m[1] + ' 分钟'; }],
    [/^Thought for (\d+)m$/, function(m) { return '思考了 ' + m[1] + ' 分钟'; }],
    [/^Thought for (\d+[hms]?\d*[ms]?)$/, function(m) { return '思考了 ' + m[1]; }],
    [/^Worked for (\d+[hms]?\d*[ms]?)$/, function(m) { return '工作了 ' + m[1]; }],
    // 命令运行数
    [/^Ran (\d+) commands?$/, function(m) { return '运行了 ' + m[1] + ' 条命令'; }],
    // 探索任务/文件数
    [/^Explored (\d+) tasks?$/, function(m) { return '探索了 ' + m[1] + ' 个任务'; }],
    [/^Explored (\d+) files?$/, function(m) { return '浏览了 ' + m[1] + ' 个文件'; }],
    // Show N breakdowns / more
    [/^Show (\d+) breakdowns?$/, function(m) { return '显示 ' + m[1] + ' 项明细'; }],
    [/^Show (\d+) more$/, function(m) { return '显示更多 ' + m[1] + ' 项'; }],
    [/^See (\d+) more plugins?$/, function(m) { return '查看更多 ' + m[1] + ' 个插件'; }],
    [/^See (\d+) more$/, function(m) { return '查看更多 ' + m[1] + ' 项'; }],
    [/^Filter skills and rules: (.+)$/, function(m) { return '筛选技能与规则：' + lookupWord(m[1]); }],
    [/^More actions for (.+)$/, function(m) { return m[1] + ' 的更多操作'; }],
    [/^Details for (.+)$/, function(m) { return m[1] + ' 详情'; }],
    [/^Global: Installed in (.+)$/, function(m) { return '全局：安装于 ' + m[1]; }],
    // N tools enabled
    [/^(\d+) tools? enabled$/, function(m) { return '已启用 ' + m[1] + ' 个工具'; }],
    // See all (N)
    [/^See all \((\d+)\)$/, function(m) { return '查看全部 (' + m[1] + ')'; }],
    // 配额重置与剩余百分比（用量面板）
    [/^Individual quota reached\. .+ Resets in (.+)\.$/, function(m) { return '个人配额已用尽。请升级订阅以提高额度。将在 ' + translateDuration(m[1]) + ' 后重置。'; }],
    [/^Resets in (.+)$/, function(m) { return translateDuration(m[1]) + '后重置'; }],
    [/^(\d+(?:\.\d+)?)% left$/, function(m) { return '剩余 ' + m[1] + '%'; }],
    // Media (date)
    [/^Media \((.+)\)$/, function(m) { return '媒体 (' + m[1] + ')'; }],
    // 排除数 / 更新时间
    [/^(\d+) excluded$/, function(m) { return '已排除 ' + m[1] + ' 项'; }],
    [/^Updated (\d+:\d+)$/, function(m) { return '更新于 ' + m[1]; }],
    // N task(s) running
    [/^(\d+) tasks? running$/, function(m) { return m[1] + ' 个任务运行中'; }],
    // 计划与步骤进度（2.34+）
    [/^Plan, (\d+) of (\d+) steps done$/, function(m) { return '计划，已完成 ' + m[1] + '/' + m[2] + ' 步'; }],
    [/^(\d+) of (\d+) steps done$/, function(m) { return '已完成 ' + m[1] + '/' + m[2] + ' 步'; }],
    [/^(\d+)\/(\d+) steps$/, function(m) { return m[1] + '/' + m[2] + ' 步'; }],
    [/^No more older messages, showing (\d+) of (\d+)$/, function(m) { return '没有更早的消息了，已显示 ' + m[1] + '/' + m[2]; }],
    // 2.34+ 选择器、设置页计数、快捷键 aria-label 与欢迎语
    [/^Welcome back, (.+)\. Where should we pick up\?$/, function(m) { return '欢迎回来，' + m[1] + '。我们从哪里继续？'; }],
    [/^Send feedback as (.+)$/, function(m) { return '以 ' + m[1] + ' 身份发送反馈'; }],
    [/^(\d+) active conversations?$/, function(m) { return m[1] + ' 个活跃对话'; }],
    [/^(\d+) archived conversations?$/, function(m) { return m[1] + ' 个已归档对话'; }],
    [/^(\d+) comments?$/, function(m) { return m[1] + ' 条评论'; }],
    [/^(\d+) hidden hosts?$/, function(m) { return m[1] + ' 个隐藏主机'; }],
    [/^(\d+) MCP servers? failed to start$/, function(m) { return m[1] + ' 个 MCP 服务器启动失败'; }],
    [/^Add shortcut for (.+)$/, function(m) { return '为「' + lookupWord(m[1]) + '」添加快捷键'; }],
    [/^Remove shortcut (.+) for (.+)$/, function(m) { return '移除「' + lookupWord(m[2]) + '」的快捷键 ' + m[1]; }],
    [/^Shortcut (.+) for (.+)$/, function(m) { return '「' + lookupWord(m[2]) + '」的快捷键 ' + m[1]; }],
    [/^Agent permissions preset selector, current: (.+)$/, function(m) { return '智能体权限预设选择器，当前：' + lookupWord(m[1]); }],
    [/^Select model, current: (.+)$/, function(m) { return '选择模型，当前：' + m[1]; }],
    [/^Select project, current: (.+)$/, function(m) { return '选择项目，当前：' + lookupWord(m[1]); }],
    [/^Where the agent runs, current: (.+)$/, function(m) { return '智能体运行位置，当前：' + lookupWord(m[1]); }],
    [/^Conversation: (.+)$/, function(m) { return '对话：' + m[1]; }],
    [/^Project: (.+)$/, function(m) { return '项目：' + m[1]; }],
    [/^(\d+) Drafts?$/, function(m) { return m[1] + ' 份草稿'; }],
    [/^Resume (\d+) comment drafts?$/, function(m) { return '继续编辑 ' + m[1] + ' 条评论草稿'; }],
    [/^Start Best-of-(\d+)$/, function(m) { return '开始 Best-of-' + m[1]; }],
    // @ / 菜单里的长描述前缀匹配（避免尾部变化导致失配）
    [/^Install or enable the Google Drive plugin in Customizations to search files with @drive:/, function() { return '在自定义中安装或启用 Google Drive 插件，即可通过 @drive: 搜索文件或粘贴链接。'; }],
    [/^\(Preview & Quota Heavy\) Run a swarm: several agents working in parallel on one task/, function() { return '（预览版·较耗配额）启动蜂群协作：多个智能体在各自工作区中并行处理同一任务。'; }],
    [/^Run a swarm: several agents working in parallel on one task/, function() { return '启动蜂群协作：多个智能体在各自工作区中并行处理同一任务。'; }],
    // 相对时间（精确单位优先）
    [/^(\d+)s ago$/, function(m) { return m[1] + '秒前'; }],
    [/^(\d+)m ago$/, function(m) { return m[1] + '分钟前'; }],
    [/^(\d+)h ago$/, function(m) { return m[1] + '小时前'; }],
    [/^(\d+)d ago$/, function(m) { return m[1] + '天前'; }],
    [/^(\d+)w ago$/, function(m) { return m[1] + '周前'; }],
    [/^(\d+)mo ago$/, function(m) { return m[1] + '个月前'; }],
    [/^(\d+[dhms]) ago$/, function(m) { return m[1] + ' 前'; }],
    [/^(\d+)mo$/, function(m) { return m[1] + '个月'; }],
  ];

  // 仅在侧边栏 / 导航区 / time 标签中生效的极简相对时间（避免误伤正文中的 "3m"、"2h" 等）
  var COMPACT_TIME_PATTERNS = [
    [/^(\d+)s$/, function(m) { return m[1] + '秒'; }],
    [/^(\d+)m$/, function(m) { return m[1] + '分钟'; }],
    [/^(\d+)h$/, function(m) { return m[1] + '小时'; }],
    [/^(\d+)d$/, function(m) { return m[1] + '天'; }],
    [/^(\d+)w$/, function(m) { return m[1] + '周'; }],
    [/^(\d+)y$/, function(m) { return m[1] + '年'; }],
  ];

  function isSidebarOrTimeEl(el) {
    if (!el) return false;
    if (el.tagName === 'TIME') return true;
    try {
      if (el.closest && el.closest('aside, nav, [class*="sidebar"], [data-sidebar]')) return true;
      // 左侧 280px 内的窄文本标签（如侧边栏对话时间列）
      if (el.tagName === 'SPAN' && el.getBoundingClientRect) {
        var r = el.getBoundingClientRect();
        if (r.width > 0 && r.width < 60 && r.left > 0 && r.right < 320) return true;
      }
    } catch (e) {}
    return false;
  }

  function tryCompactTime(text, parentEl) {
    if (!isSidebarOrTimeEl(parentEl)) return null;
    var trimmed = String(text).trim();
    for (var i = 0; i < COMPACT_TIME_PATTERNS.length; i++) {
      var m = trimmed.match(COMPACT_TIME_PATTERNS[i][0]);
      if (m) return text.replace(trimmed, COMPACT_TIME_PATTERNS[i][1](m));
    }
    return null;
  }

  function tryPatternMatch(text) {
    for (var i = 0; i < PATTERNS.length; i++) {
      var m = text.match(PATTERNS[i][0]);
      if (m) return PATTERNS[i][1](m);
    }
    return null;
  }

  function tryTranslate(text) {
    // 关键：把不换行空格(U+00A0/U+202F/U+2007)与其它 Unicode 空白统一成普通空格。
    // Antigravity 的不少设置标签用的是 U+00A0（如 "Queued Messages"、"Prevent Sleep"），
    // 不归一化就会导致词典键匹配不上——原版 observer.js 在这里全军覆没。
    var normalized = String(text)
      .replace(/[\u00a0\u2000-\u200a\u202f\u205f\u3000]/g, ' ');
    var trimmed = normalized.trim();
    if (!trimmed || trimmed.length < 2) return null;
    if (!/[a-zA-Z]/.test(trimmed)) return null;

    // 1) 精确命中（与原版行为一致，只是先做了空白归一化）
    if (Object.prototype.hasOwnProperty.call(dict, trimmed)) {
      return normalized.replace(trimmed, dict[trimmed]);
    }
    // 1.2) 大小写不敏感精确兜底（如 "Voice mode" 命中 "Voice Mode"）
    // 关键保护：纯小写单词（如斜杠命令名 compact / browser / plan）绝不走大小写兜底，
    // 防止 /compact、/browser、/plan 等命令名被误翻成中文。
    if ((trimmed.length >= 4 || trimmed.indexOf(' ') !== -1) && !/^[a-z0-9_-]+$/.test(trimmed)) {
      var lower = trimmed.toLowerCase();
      if (Object.prototype.hasOwnProperty.call(dictLower, lower)) {
        return normalized.replace(trimmed, dictLower[lower]);
      }
    }
    // 1.5) 正则模式匹配：处理含动态数字/时间的 UI 文本
    var patternResult = tryPatternMatch(trimmed);
    if (patternResult !== null) {
      return normalized.replace(trimmed, patternResult);
    }
    // 2) 短语切分兜底（原版在这里直接放弃）
    if (trimmed.length > 120) return null;          // 长句几乎不可能是 UI 文案
    if (looksLikeCode(trimmed) || looksLikeData(trimmed)) return null;
    return translatePhrase(normalized);
  }

  // ====== 状态 ======
  var textSeen = HAS_WEAKSET ? new WeakSet() : null;     // 已处理过的文本节点
  var textOrigin = HAS_WEAKMAP ? new WeakMap() : null;   // 节点 → 我方写入的译文
  var attrOrigin = HAS_WEAKMAP ? new WeakMap() : null;   // 元素 → {属性名: 我方写入值}
  var touchedText = 0, touchedAttr = 0, cjkFixed = 0, restored = 0;
  // 原文登记表：用于「关掉翻译时还原英文」。WeakMap 无法遍历，故用 Map。
  var textOrigins = (typeof Map === 'function') ? new Map() : null;
  var attrOrigins = (typeof Map === 'function') ? new Map() : null;

  // ====== 跨文本节点整句修复（如 React 拆分的 "No " + "automations" + " yet"）======
  var INLINE_CONTAINER_TAGS = { P: 1, DIV: 1, SPAN: 1, H1: 1, H2: 1, H3: 1, H4: 1, H5: 1, H6: 1, LABEL: 1, LI: 1 };
  function tryTranslateSplitContainer(el) {
    if (!el || el.nodeType !== 1 || !INLINE_CONTAINER_TAGS[el.tagName]) return false;
    if (isSkippableParent(el)) return false;
    var children = el.childNodes;
    if (!children || children.length < 2 || children.length > 6) return false;
    // 只处理全部由纯文本节点或注释节点组成的容器（不破坏包含按钮/图标/链接的子元素）
    var textNodes = [];
    for (var i = 0; i < children.length; i++) {
      var c = children[i];
      if (c.nodeType === 3) textNodes.push(c);
      else if (c.nodeType !== 8) return false;
    }
    if (textNodes.length < 2) return false;
    var combined = '';
    for (var j = 0; j < textNodes.length; j++) combined += (textNodes[j].textContent || '');
    var trimmed = combined.replace(/[\u00a0\u2000-\u200a\u202f\u205f\u3000]/g, ' ').replace(/\s+/g, ' ').trim();
    if (!trimmed || trimmed.length > 140 || !/[A-Za-z]{2,}/.test(trimmed)) return false;

    var res = null;
    if (Object.prototype.hasOwnProperty.call(dict, trimmed)) res = dict[trimmed];
    else if (Object.prototype.hasOwnProperty.call(dictLower, trimmed.toLowerCase())) res = dictLower[trimmed.toLowerCase()];
    else res = tryPatternMatch(trimmed);

    if (res === null || res === trimmed) return false;
    for (var k = 0; k < textNodes.length; k++) {
      var tn = textNodes[k];
      if (textOrigins && !textOrigins.has(tn)) textOrigins.set(tn, tn.textContent || '');
      tn.textContent = (k === 0) ? res : '';
      if (textOrigin) textOrigin.set(tn, tn.textContent);
      if (textSeen) textSeen.add(tn);
    }
    touchedText++;
    return true;
  }

  // 特殊语境：模型列表等处的 "New" 徽章（非 "+ New" 新建按钮）应显示为 "新" 而非 "新建"
  function fixNewBadge(cur, trimmedCur, result, parent) {
    if (trimmedCur !== 'New' || result !== '新建' || !parent) return result;
    var pText = (parent.textContent || '').trim();
    if (pText.indexOf('+') !== -1) return result;
    var gp = parent.parentElement;
    if (gp && gp.textContent && gp.textContent.indexOf('+') !== -1) return result;
    var inMenu = !!(parent.closest && parent.closest('[role="menuitem"], [role="option"], [role="listbox"], [role="menu"]'));
    var hasInfoBadge = !!(gp && gp.querySelector && gp.querySelector('svg'));
    var rowEl = parent.closest ? parent.closest('button, li, [role="menuitem"], [role="option"]') : null;
    var isBadgeInRow = !!(rowEl && (rowEl.textContent || '').trim().length > 8);
    if ((inMenu || hasInfoBadge || isBadgeInRow) && parent.tagName !== 'BUTTON') {
      return cur.replace('New', '新');
    }
    return result;
  }

  // ====== 文本节点 ======
  function processTextNode(node) {
    if (!node || node.nodeType !== 3) return;
    var cur = node.textContent || '';

    // 我方写入过，且当前值仍是我方写入值 → 是我们自己造成的变更，跳过
    if (textOrigin) {
      var mine = textOrigin.get(node);
      if (mine !== undefined && mine === cur) return;
    } else if (textSeen && textSeen.has(node)) {
      return;
    }

    var parent = node.parentElement;
    if (isSkippableParent(parent)) {
      if (textSeen) textSeen.add(node);
      return;
    }

    // 优先尝试跨节点整句匹配（防止 "No " + "automations" + " yet" 被拆翻）
    if (parent && parent.childNodes && parent.childNodes.length > 1) {
      if (tryTranslateSplitContainer(parent)) return;
    }

    // 极短高歧义词保护：若包含相邻拆句节点或带前后空格，严禁单独翻译（防 "No " → "否 "）
    var trimmedCur = cur.trim();
    if (AMBIGUOUS_SHORT[trimmedCur]) {
      var hasSiblingText = parent && parent.childNodes && parent.childNodes.length > 1;
      if (hasSiblingText || cur !== trimmedCur) {
        if (textOrigin) textOrigin.set(node, cur);
        if (textSeen) textSeen.add(node);
        return;
      }
    }

    var result = tryTranslate(cur);
    if (result === null) result = tryCompactTime(cur, parent);
    result = fixNewBadge(cur, trimmedCur, result, parent);
    if (result !== null && result !== cur) {
      if (textOrigins && !textOrigins.has(node)) textOrigins.set(node, cur);   // 只记首次见到的原文
      node.textContent = result;
      touchedText++;
      if (textOrigin) textOrigin.set(node, result);
    } else if (textOrigin) {
      textOrigin.set(node, cur);   // 记住原值，避免同一个未命中串被反复尝试
    }
    if (textSeen) textSeen.add(node);
    else node[MARK] = true;
  }

  // ====== 元素属性 ======
  function processElementAttrs(el) {
    if (!el || el.nodeType !== 1 || !el.getAttribute) return;
    for (var i = 0; i < TR_ATTRS.length; i++) {
      var name = TR_ATTRS[i];
      var val = el.getAttribute(name);
      if (!val) continue;
      var trimmed = val.trim();
      if (trimmed.length < 2) continue;

      var store = attrOrigin ? attrOrigin.get(el) : null;
      if (store && store[name] === trimmed) continue;   // 我方写入 → 跳过（防死循环）

      var next = tryTranslate(val);
      if (next !== null) next = next.trim();

      if (next === null || next === trimmed) { if (store) store[name] = trimmed; continue; }
      if (attrOrigin) {
        if (!store) { store = {}; attrOrigin.set(el, store); }
        store[name] = next;
      }
      if (attrOrigins) {
        var am = attrOrigins.get(el);
        if (!am) { am = {}; attrOrigins.set(el, am); }
        if (!(name in am)) am[name] = val;          // 只记第一次的英文原文
      }
      el.setAttribute(name, next);
      touchedAttr++;
    }
  }

  // ====== 修复旧引擎遗留的「中文 空格 中文」 ======
  // 早期版本会把 "Conversation History" 渲染成「对话 历史」；那些节点已被旧引擎
  // 标记、内容也不再变化，新引擎不会重新处理它们。这里做一次遍历把词间空格抹掉。
  // 只动「中文之间的空白」，不碰任何其它字符，因此对正文/代码无风险。
  var CJK_SPACE_RE = /([\u4e00-\u9fa5])[ \t\u00a0]+(?=[\u4e00-\u9fa5])/g;
  function repairCjkSpacing() {
    if (!document.body) return 0;
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
    var n, fixed = 0;
    while ((n = walker.nextNode())) {
      var t = n.textContent || '';
      if (t.indexOf(' ') === -1 && t.indexOf('\t') === -1 && t.indexOf('\u00a0') === -1) continue;
      var nv = t.replace(CJK_SPACE_RE, '$1');
      if (nv !== t) { n.textContent = nv; fixed++; }
    }
    return fixed;
  }

  // ====== 遍历 ======
  function walkAndTranslate(root, deadline) {
    if (!root) return;
    if (root.nodeType === 3) { processTextNode(root); return; }
    if (root.nodeType !== 1 && root.nodeType !== 11) return;
    if (root.nodeType === 1 && SKIP_TAGS[root.tagName]) return;

    if (root.nodeType === 1) processElementAttrs(root);

    var walker = document.createTreeWalker(
      root,
      NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT,
      null,
      false
    );
    var n, scanned = 0;
    while ((n = walker.nextNode())) {
      if (window.__agy_zh_enabled === false) return;
      if (++scanned > CFG.maxScanNodes) break;
      if (deadline && (scanned & 255) === 0 && Date.now() > deadline) break;
      if (n.nodeType === 3) processTextNode(n);
      else if (!SKIP_TAGS[n.tagName]) processElementAttrs(n);
    }
  }

  // ====== 批量队列：空闲时处理，不阻塞 UI ======
  var pending = [], scheduled = false;

  function flush() {
    scheduled = false;
    var batch = pending;
    pending = [];
    var deadline = Date.now() + CFG.budgetMs;
    for (var i = 0; i < batch.length; i++) {
      if (window.__agy_zh_enabled === false) return;
      if (Date.now() > deadline) { pending = batch.slice(i).concat(pending); schedule(); syncStats(); return; }
      walkAndTranslate(batch[i], deadline);
    }
    syncStats();
  }
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    if (window.requestIdleCallback) { requestIdleCallback(flush, { timeout: 300 }); return; }
    if (window.requestAnimationFrame) { requestAnimationFrame(flush); return; }
    // 兜底：两个调度 API 都不可用时同步处理，避免漏翻译
    flush();
  }
  function enqueue(node) {
    if (pending.length > 5000) { walkAndTranslate(node, Date.now() + CFG.budgetMs); return; }
    pending.push(node);
    schedule();
  }

  // ====== 统计（供外部审计）======
  function syncStats() {
    window.__agy_zh_stats = {
      engine: 'observer_plus',
      version: ENGINE_VERSION,
      dictSig: DICT_SIG,
      cjkSpacingFixed: cjkFixed,
      textRestored: restored,
      dictSize: dictKeys.length,
      textNodesTranslated: touchedText,
      attrsTranslated: touchedAttr,
      pending: pending.length,
      enabled: window.__agy_zh_enabled !== false,
      watchingAttributes: true,
      strict: !CFG.allowPartialPhrases,
      hud: true
    };
  }

  // ====== 上下文窗口与 Token 生成速度 HUD（内嵌到「选择模型」按钮旁，Shadow DOM 隔离）======
  var hudHost = null;
  var hudShadow = null;
  var hudWrap = null;
  var hudPayload = null;
  var hudActiveConvId = null;
  var hudLastRouteId = undefined;
  var hudExpanded = false;
  var hudCapOverride = 0; // 0 = 自动
  var hudEnabled = true;
  var hudThemeTimer = null;
  var hudStreamTimer = null;
  var hudRemountTimer = null;
  var hudResizeHandler = null;
  var hudDocClickHandler = null;
  var hudKeyHandler = null;
  var hudVisHandler = null;
  var hudAnchorBtn = null;
  var isRestoring = false;
  var domDirty = true;
  var lastRescanAt = 0;

  // DOM 流式输出实时测速状态
  var streamEvents = []; // [{t, tok}]
  var streamNodeLens = HAS_WEAKMAP ? new WeakMap() : null;
  var streamActive = false;
  var streamLastTps = null;
  var streamEmaTps = null;
  var streamLastAt = 0;
  var streamBaselineUpdatedAt = 0;
  var streamWasWorking = false;
  var tempIdleTps = null;
  var domWorkCache = { ts: 0, val: false };

  function loadStorage(key, fallback) {
    try {
      var v = window.localStorage ? window.localStorage.getItem(key) : null;
      return v !== null ? v : fallback;
    } catch (e) { return fallback; }
  }
  function saveStorage(key, val) {
    try { if (window.localStorage) window.localStorage.setItem(key, String(val)); } catch (e) {}
  }

  function formatTok(n) {
    if (n === null || n === undefined || isNaN(n)) return '-';
    if (n >= 1000000) return (n / 1000000).toFixed(n % 1000000 === 0 ? 0 : 2) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return String(Math.round(n));
  }
  function formatExact(n) {
    if (n === null || n === undefined || isNaN(n)) return '-';
    return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function detectDarkTheme() {
    try {
      var root = document.documentElement;
      if (root && root.classList) {
        if (root.classList.contains('light')) return false;
        if (root.classList.contains('dark')) return true;
      }
      if (document.body && window.getComputedStyle) {
        var bg = window.getComputedStyle(document.body).backgroundColor || '';
        var m = bg.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
        if (m) {
          var lum = 0.299 * (+m[1]) + 0.587 * (+m[2]) + 0.114 * (+m[3]);
          return lum < 140;
        }
      }
    } catch (e) {}
    return true;
  }

  function estimateTokensFromText(txt) {
    if (!txt) return 0;
    var cjk = (txt.match(/[\u4e00-\u9fa5]/g) || []).length;
    var rest = Math.max(0, txt.length - cjk);
    return cjk * 0.67 + rest * 0.25;
  }

  function isInMainChatArea(el) {
    if (!el) return false;
    try {
      if (el.id === '__agy_metrics_host') return false;
      if (el.closest) {
        if (el.closest('#__agy_metrics_host, aside, nav, [class*="sidebar"], [data-sidebar], [data-sidebar-row-id], [data-sidebar-section-id], .agy-row-actions')) return false;
      }
    } catch (e) {}
    return true;
  }

  function detectDomWorking() {
    var now = Date.now();
    if (now - domWorkCache.ts < 150) return domWorkCache.val;
    var working = false;
    var STOP_RE = /^(Cancel|Stop|Stop generating|Stop execution|Stop this agent|Stopping|Interrupt the agent|取消|停止$|停止生成|停止执行|停止此智能体|正在停止|中断智能体)/i;
    var EXCLUDE_RE = /(Voice|语音|Editing|编辑|Side|侧边)/i;
    try {
      // 1) 最精准信号：Antigravity 输入框右下角运行态的取消/停止按钮
      var cancelBtn = document.querySelector && document.querySelector('[data-testid="input-cancel-button"]');
      if (cancelBtn && isVisibleButton(cancelBtn)) {
        working = true;
      }
      // 1.5) 用户在 AI 运行期间往输入框打字时，取消按钮会临时变成发送/排队箭头，
      // 但权限选择按钮会保持 disabled 且 aria-label 提示 "while the agent is running"
      if (!working && document.querySelector) {
        var permBtn = document.querySelector('[data-testid="permissions-selector-trigger"]');
        if (permBtn && permBtn.isConnected) {
          var pAria = (permBtn.getAttribute('aria-label') || '') + ' ' + ((attrOrigins && attrOrigins.get(permBtn) && attrOrigins.get(permBtn)['aria-label']) || '');
          if (permBtn.disabled || permBtn.getAttribute('aria-disabled') === 'true' || /while the agent is running/i.test(pAria)) {
            working = true;
          }
        }
      }
      // 2) 兜底：在当前输入框整栏容器（向上最多 8 层，高度 <= 420px）内匹配取消/停止按钮
      if (!working && hudAnchorBtn && hudAnchorBtn.isConnected) {
        var scope = null;
        var box = hudAnchorBtn.parentElement;
        for (var up = 0; up < 8 && box && box.parentElement; up++) {
          if (box.offsetHeight && box.offsetHeight > 420) break;
          scope = box;
          box = box.parentElement;
        }
        var btns = scope && scope.querySelectorAll ? scope.querySelectorAll('button, [role="button"]') : [];
        for (var i = 0; i < btns.length; i++) {
          var b = btns[i];
          if (!isVisibleButton(b)) continue;
          var aria = (b.getAttribute && b.getAttribute('aria-label')) || '';
          var title = (b.getAttribute && b.getAttribute('title')) || '';
          var origMap = attrOrigins ? attrOrigins.get(b) : null;
          var origAria = (origMap && origMap['aria-label']) ? origMap['aria-label'] : '';
          var origTitle = (origMap && origMap['title']) ? origMap['title'] : '';
          var candidates = [aria, title, origAria, origTitle];
          for (var c = 0; c < candidates.length; c++) {
            var s = candidates[c].trim();
            if (!s) continue;
            if (!EXCLUDE_RE.test(s) && STOP_RE.test(s)) {
              working = true;
              break;
            }
          }
          if (working) break;
        }
      }
    } catch (e) {}
    domWorkCache.ts = now;
    domWorkCache.val = working;
    return working;
  }

  function recordStreamingMutation(mu) {
    if (!hudHost || !hudEnabled) return;
    var now = Date.now();
    var deltaChars = 0;
    var deltaTok = 0;
    if (mu.type === 'characterData') {
      var tn = mu.target;
      if (!tn || tn.nodeType !== 3) return;
      var cur = tn.textContent || '';
      if (textOrigin && textOrigin.get(tn) === cur) return; // 我方翻译写入
      if (!isInMainChatArea(tn.parentElement)) return;
      var prevLen = streamNodeLens ? (streamNodeLens.get(tn) || 0) : 0;
      if (streamNodeLens) streamNodeLens.set(tn, cur.length);
      var diff = cur.length - prevLen;
      if (diff > 0 && diff < 800) {
        deltaChars = diff;
        deltaTok = estimateTokensFromText(cur.slice(prevLen));
      }
    } else if (mu.type === 'childList' && mu.addedNodes && mu.addedNodes.length) {
      for (var i = 0; i < mu.addedNodes.length; i++) {
        var nd = mu.addedNodes[i];
        if (!nd) continue;
        if (nd.id === '__agy_metrics_host') continue;
        var parentEl = nd.nodeType === 1 ? nd : nd.parentElement;
        if (!isInMainChatArea(parentEl)) continue;
        var txt = nd.textContent || '';
        if (txt.length > 0 && txt.length < 800) {
          if (nd.nodeType === 3) {
            var pLen = streamNodeLens ? (streamNodeLens.get(nd) || 0) : 0;
            if (streamNodeLens) streamNodeLens.set(nd, txt.length);
            var df = txt.length - pLen;
            if (df > 0) {
              deltaChars += df;
              deltaTok += estimateTokensFromText(txt.slice(pLen));
            }
          } else {
            deltaChars += txt.length;
            deltaTok += estimateTokensFromText(txt);
          }
        }
      }
    }
    if (deltaChars > 0 && deltaChars < 800 && deltaTok > 0) {
      streamEvents.push({ t: now, tok: deltaTok });
      streamLastAt = now;
      while (streamEvents.length && now - streamEvents[0].t > 2800) streamEvents.shift();
      var recentCount = 0;
      for (var j = streamEvents.length - 1; j >= 0; j--) {
        if (now - streamEvents[j].t <= 1800) recentCount++;
        else break;
      }
      if (recentCount >= 2 && (now - streamEvents[0].t) >= 250) {
        streamActive = true;
      }
    }
  }

  function getRouteConvId() {
    try {
      var pathname = String(location.pathname || '');
      var m = pathname.match(/\/c\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i);
      return m ? m[1].toLowerCase() : null;
    } catch (e) {
      return null;
    }
  }

  function inferLimitFromModelText(name) {
    var s = String(name || '').toLowerCase();
    if (s.indexOf('claude') !== -1) return 200000;
    if (s.indexOf('flash') !== -1) return 256000;
    if (s.indexOf('gpt') !== -1) return 272000;
    if (s.indexOf('neon') !== -1) return 512000;
    return 1000000;
  }

  function inferAnchorModelInfo() {
    var name = '';
    try {
      if (hudAnchorBtn && hudAnchorBtn.isConnected) {
        name = String(hudAnchorBtn.textContent || '').replace(/\s+/g, ' ').trim();
      }
    } catch (e) {}
    return {
      name: name || '新对话',
      limit: inferLimitFromModelText(name)
    };
  }

  function resetStreamState() {
    streamEvents = [];
    streamActive = false;
    streamLastTps = null;
    streamEmaTps = null;
    streamLastAt = 0;
    streamBaselineUpdatedAt = 0;
    streamWasWorking = false;
    tempIdleTps = null;
  }

  function syncRouteChange() {
    var routeId = getRouteConvId();
    if (routeId !== hudLastRouteId) {
      hudLastRouteId = routeId;
      hudActiveConvId = routeId;
      resetStreamState();
      return true;
    }
    return false;
  }

  function updateStreamSpeed() {
    var routeChanged = syncRouteChange();
    var now = Date.now();
    var m = pickActiveConversation(hudPayload);
    var isWorking = detectDomWorking() || !!(m && m.isWorking);

    if (routeChanged) {
      renderHud();
    }

    if (!streamWasWorking && isWorking) {
      streamEmaTps = null;
      streamLastTps = null;
      streamEvents = [];
      streamActive = false;
      tempIdleTps = null;
    } else if (streamWasWorking && !isWorking) {
      streamActive = false;
      streamLastTps = null;
      streamEmaTps = null;
      streamEvents = [];
      renderHud();
    }
    streamWasWorking = isWorking;

    if (!streamActive) {
      if (isWorking) renderHud();
      return;
    }
    if (now - streamLastAt > 2000) {
      streamActive = false;
      streamLastTps = null;
      streamEmaTps = null;
      streamEvents = [];
      renderHud();
      return;
    }
    while (streamEvents.length && now - streamEvents[0].t > 2500) streamEvents.shift();
    if (streamEvents.length >= 2) {
      var span = Math.max(0.25, (now - streamEvents[0].t) / 1000);
      var sum = 0;
      for (var i = 0; i < streamEvents.length; i++) sum += streamEvents[i].tok;
      var rawTps = Math.max(1, sum / span);
      streamEmaTps = streamEmaTps ? (0.6 * streamEmaTps + 0.4 * rawTps) : rawTps;
      streamLastTps = Math.max(1, Math.round(streamEmaTps));
      renderHud();
    }
  }

  function pickActiveConversation(payload) {
    var routeId = getRouteConvId();
    // 严格按当前路由 /c/<uuid> 匹配，绝不将其他会话数据串到新建空对话或别的会话上
    if (!routeId) {
      hudActiveConvId = null;
      return null;
    }
    if (!payload) return null;

    if (payload.primary && payload.primary.convId && payload.primary.convId.toLowerCase() === routeId) {
      hudActiveConvId = payload.primary.convId;
      return payload.primary;
    }

    var cands = payload.candidates || [];
    for (var i = 0; i < cands.length; i++) {
      if (cands[i].convId && cands[i].convId.toLowerCase() === routeId) {
        hudActiveConvId = cands[i].convId;
        return cands[i];
      }
    }

    hudActiveConvId = routeId;
    return null;
  }

  function isVisibleButton(el) {
    if (!el || !el.isConnected) return false;
    try {
      if (el.id === '__agy_metrics_host') return false;
      if (el.closest && el.closest('#__agy_metrics_host, aside, nav, [data-sidebar-row-id], [data-sidebar-section-id], .agy-row-actions, [role="dialog"], [role="menu"], [role="listbox"]')) return false;
      if (el.getBoundingClientRect) {
        var r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return false;
      }
    } catch (e) {}
    return true;
  }

  function findModelSelectorAnchor() {
    if (!document.querySelectorAll) return null;
    var LABEL_RE = /^(Select model|Select a model|Change model|Model Selection|选择模型|切换模型)/i;
    var TEXT_RE = /(Gemini|Claude|GPT|Carbon|Neon|Hydrogen|Flash|Sonnet|Opus|Orchestrator|编排器|Select [Mm]odel|选择模型)/;
    var modelBtn = null;

    try {
      var btns = document.querySelectorAll('button, [role="button"]');
      for (var i = 0; i < btns.length; i++) {
        var b = btns[i];
        if (!isVisibleButton(b)) continue;
        var aria = (b.getAttribute && b.getAttribute('aria-label')) || '';
        var origMap = attrOrigins ? attrOrigins.get(b) : null;
        var origAria = (origMap && origMap['aria-label']) ? origMap['aria-label'] : '';
        if (LABEL_RE.test(aria.trim()) || LABEL_RE.test(origAria.trim())) {
          modelBtn = b;
          break;
        }
      }

      if (!modelBtn) {
        var inputs = document.querySelectorAll('[aria-label="Message input"], [aria-label="消息输入框"], textarea, [contenteditable="true"]');
        for (var j = 0; j < inputs.length; j++) {
          var box = inputs[j];
          for (var depth = 0; depth < 5 && box && box.parentElement; depth++) {
            box = box.parentElement;
            if (box.offsetHeight && box.offsetHeight > 420) break;
            if (!box.querySelectorAll) continue;
            var candBtns = box.querySelectorAll('button, [role="button"]');
            for (var k = 0; k < candBtns.length; k++) {
              var cb = candBtns[k];
              if (!isVisibleButton(cb)) continue;
              var txt = (cb.textContent || '').trim();
              if (txt && txt.length < 64 && TEXT_RE.test(txt)) {
                modelBtn = cb;
                break;
              }
            }
            if (modelBtn) break;
          }
          if (modelBtn) break;
        }
      }
    } catch (e) {}

    if (!modelBtn || !modelBtn.parentElement) return null;

    var afterNode = modelBtn;
    var p = modelBtn.parentElement;
    for (var up = 0; up < 2 && p && p.parentElement; up++) {
      var childCount = 0;
      var ch = p.children || p.childNodes || [];
      for (var c = 0; c < ch.length; c++) {
        var cn = ch[c];
        if (cn && cn.nodeType === 1 && cn.id !== '__agy_metrics_host') childCount++;
      }
      var disp = '';
      try {
        if (window.getComputedStyle) disp = window.getComputedStyle(p).display || '';
      } catch (e) {}
      if (childCount <= 1 || disp === 'contents') {
        afterNode = p;
        p = p.parentElement;
      } else {
        break;
      }
    }

    if (!p || !p.insertBefore) return null;
    return { row: p, afterNode: afterNode, modelBtn: modelBtn };
  }

  function positionPopover() {
    if (!hudShadow || !hudHost || !hudExpanded) return;
    var popEl = hudShadow.getElementById('agy-pop');
    if (!popEl || !hudHost.getBoundingClientRect) return;
    var rect = hudHost.getBoundingClientRect();
    var vw = window.innerWidth || 1200;
    var vh = window.innerHeight || 800;
    var popW = 364;
    var left = Math.max(8, Math.min(rect.left, vw - popW - 8));
    popEl.style.left = Math.round(left) + 'px';
    if (rect.top < 320) {
      popEl.style.bottom = 'auto';
      popEl.style.top = Math.round(rect.bottom + 8) + 'px';
    } else {
      popEl.style.top = 'auto';
      popEl.style.bottom = Math.round(vh - rect.top + 8) + 'px';
    }
  }

  function createHudHost() {
    if (hudHost || typeof document.createElement !== 'function') return;
    hudCapOverride = parseInt(loadStorage('__agy_hud_ctx_cap', '0'), 10) || 0;

    hudHost = document.createElement('div');
    hudHost.id = '__agy_metrics_host';
    hudHost.style.cssText = 'display:inline-flex;align-items:center;height:28px;min-width:0;flex-shrink:1;margin-left:2px;user-select:none;font-family:inherit;';
    if (typeof hudHost.attachShadow === 'function') {
      hudShadow = hudHost.attachShadow({ mode: 'open' });
    } else {
      hudShadow = hudHost;
    }

    hudShadow.innerHTML =
      '<style>' +
      ':host{--agy-accent:#3b82f6;--agy-accent-soft:rgba(59,130,246,0.16);--agy-accent-mid:rgba(59,130,246,0.45);}' +
      '.wrap{position:relative;display:inline-flex;align-items:center;height:28px;font-size:12px;line-height:16px;color:var(--agy-hud-fg,rgba(226,232,240,0.78));}' +
      '.wrap.light{color:var(--agy-hud-fg,rgba(30,41,59,0.75));}' +
      '.chip{display:inline-flex;align-items:center;gap:5px;padding:0 7px;height:28px;border:none;outline:none;border-radius:6px;background:transparent;color:inherit;font:inherit;line-height:16px;cursor:pointer;white-space:nowrap;transition:background .12s,color .12s;}' +
      '.chip svg{display:block;flex-shrink:0;}' +
      '.chip span{line-height:16px;}' +
      '.chip-num{font-variant-numeric:tabular-nums;}' +
      '.chip:focus{outline:none;}' +
      '.chip:focus-visible{box-shadow:0 0 0 1.5px rgba(148,163,184,0.45);}' +
      '.chip:hover,.wrap.expanded .chip{background:rgba(148,163,184,0.12);color:#f8fafc;}' +
      '.wrap.light .chip:hover,.wrap.light.expanded .chip{background:rgba(15,23,42,0.06);color:#0f172a;}' +
      '.dot{width:6px;height:6px;border-radius:50%;display:inline-block;flex-shrink:0;}' +
      '.dot.live{background:#3b82f6;box-shadow:0 0 0 2px rgba(59,130,246,0.22);animation:soft-pulse 1.4s ease-in-out infinite;}' +
      '.dot.bolt{display:none;}' +
      '.dot.idle{display:none;}' +
      '@keyframes soft-pulse{0%,100%{opacity:1;}50%{opacity:.4;}}' +
      '.mid-dot{opacity:.38;padding:0 1px;}' +
      '.mono{font-family:inherit;font-variant-numeric:tabular-nums;}' +
      '.pop{display:none;position:fixed;width:360px;padding:12px 14px;border-radius:12px;background:rgba(20,23,31,0.96);backdrop-filter:blur(18px);border:1px solid rgba(255,255,255,0.10);box-shadow:0 12px 32px rgba(0,0,0,0.42);z-index:2147483000;color:#e5e7eb;font-family:inherit;}' +
      '.wrap.light .pop{background:rgba(255,255,255,0.98);border-color:rgba(15,23,42,0.11);box-shadow:0 12px 32px rgba(15,23,42,0.12);color:#111827;}' +
      '.wrap.expanded .pop{display:block;}' +
      '.pop-head{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px;}' +
      '.head-left{display:flex;align-items:center;gap:6px;min-width:0;}' +
      '.model-name{font-weight:600;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}' +
      '.conv-tag{font-size:10px;padding:1px 5px;border-radius:4px;background:rgba(148,163,184,0.12);opacity:.62;font-family:inherit;font-variant-numeric:tabular-nums;flex-shrink:0;}' +
      '.head-right{display:flex;align-items:center;gap:6px;flex-shrink:0;}' +
      '.zh-toggle{border:1px solid rgba(148,163,184,0.28);background:transparent;color:inherit;border-radius:999px;padding:1.5px 7px;font-size:10px;line-height:1.4;cursor:pointer;opacity:.82;transition:all .12s;white-space:nowrap;}' +
      '.zh-toggle:hover{opacity:1;background:rgba(148,163,184,0.16);}' +
      '.zh-toggle.en{border-color:rgba(245,158,11,0.45);color:#fbbf24;opacity:1;}' +
      '.wrap.light .zh-toggle.en{color:#b45309;border-color:rgba(180,83,9,0.35);}' +
      '.status-tag{display:inline-flex;align-items:center;gap:5px;font-size:10px;padding:2px 7px;border-radius:999px;background:rgba(148,163,184,0.12);opacity:.78;white-space:nowrap;flex-shrink:0;}' +
      '.status-tag.live{background:rgba(59,130,246,0.14);color:#60a5fa;opacity:1;}' +
      '.wrap.light .status-tag.live{background:rgba(37,99,235,0.09);color:#2563eb;}' +
      '.ctx-block{padding:9px 10px;border-radius:9px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);margin-bottom:9px;}' +
      '.wrap.light .ctx-block{background:#f8fafc;border-color:rgba(15,23,42,0.08);}' +
      '.ctx-row{display:flex;justify-content:space-between;align-items:baseline;font-size:11px;margin-bottom:6px;}' +
      '.ctx-title{font-weight:500;opacity:.78;}' +
      '.stack-track{display:flex;height:6px;border-radius:999px;background:rgba(148,163,184,0.18);overflow:hidden;margin-bottom:7px;gap:1px;}' +
      '.stack-seg{height:100%;transition:width .25s ease;min-width:0;}' +
      '.seg-cache{background:#3b82f6;}' +
      '.wrap.light .seg-cache{background:#2563eb;}' +
      '.seg-input{background:rgba(96,165,250,0.48);}' +
      '.wrap.light .seg-input{background:#93c5fd;}' +
      '.seg-out{background:rgba(148,163,184,0.55);}' +
      '.wrap.light .seg-out{background:#cbd5e1;}' +
      '.stack-track.warn .seg-cache{background:#f59e0b;}' +
      '.stack-track.danger .seg-cache{background:#ef4444;}' +
      '.legend-row{display:flex;flex-wrap:wrap;gap:10px;font-size:10px;opacity:.72;margin-bottom:7px;}' +
      '.leg{display:inline-flex;align-items:center;gap:4px;}' +
      '.leg-dot{width:6px;height:6px;border-radius:50%;display:inline-block;}' +
      '.cap-row{display:flex;align-items:center;justify-content:space-between;font-size:10px;padding-top:6px;border-top:1px solid rgba(148,163,184,0.12);}' +
      '.wrap.light .cap-row{border-top-color:rgba(15,23,42,0.07);}' +
      '.cap-label{opacity:.65;}' +
      '.cap-group{display:flex;gap:3px;}' +
      '.cap-btn{border:1px solid transparent;background:transparent;color:inherit;opacity:.72;border-radius:5px;padding:1.5px 6px;font-size:10px;cursor:pointer;transition:all .12s;}' +
      '.cap-btn:hover{opacity:1;background:rgba(148,163,184,0.12);}' +
      '.cap-btn.active{opacity:1;background:#f3f4f6;color:#111827;font-weight:600;}' +
      '.wrap.light .cap-btn.active{background:#111827;color:#ffffff;}' +
      '.cap-note{font-size:9.5px;opacity:.5;line-height:1.35;margin-top:5px;}' +
      '.tele-grid{display:grid;grid-template-columns:1fr 1fr;border:1px solid rgba(255,255,255,0.08);border-radius:9px;overflow:hidden;margin-bottom:9px;}' +
      '.wrap.light .tele-grid{border-color:rgba(15,23,42,0.08);}' +
      '.tele-cell{padding:6px 9px;background:transparent;}' +
      '.tele-cell:nth-child(1),.tele-cell:nth-child(2){border-bottom:1px solid rgba(255,255,255,0.07);}' +
      '.tele-cell:nth-child(odd){border-right:1px solid rgba(255,255,255,0.07);}' +
      '.wrap.light .tele-cell:nth-child(1),.wrap.light .tele-cell:nth-child(2){border-bottom-color:rgba(15,23,42,0.07);}' +
      '.wrap.light .tele-cell:nth-child(odd){border-right-color:rgba(15,23,42,0.07);}' +
      '.tele-cell .k{font-size:9.5px;opacity:.58;margin-bottom:2px;}' +
      '.tele-cell .v{font-size:12px;font-weight:600;font-variant-numeric:tabular-nums;}' +
      '.steps-title{display:flex;justify-content:space-between;align-items:center;font-size:10.5px;font-weight:600;opacity:.78;margin-bottom:4px;}' +
      '.steps-hint{font-size:9.5px;font-weight:400;opacity:.5;}' +
      '.steps-list{display:flex;flex-direction:column;gap:2px;font-size:10px;font-family:inherit;font-variant-numeric:tabular-nums;}' +
      '.step-head{display:grid;grid-template-columns:46px 34px 46px 42px 34px 1fr;gap:4px;padding:2px 4px;font-size:9px;opacity:.48;font-family:inherit;border-bottom:1px solid rgba(148,163,184,0.12);margin-bottom:2px;}' +
      '.wrap.light .step-head{border-bottom-color:rgba(15,23,42,0.08);}' +
      '.step-row{display:grid;grid-template-columns:46px 34px 46px 42px 34px 1fr;align-items:center;gap:4px;padding:2.5px 4px;border-radius:4px;}' +
      '.step-row:nth-child(odd){background:rgba(148,163,184,0.05);}' +
      '.wrap.light .step-row:nth-child(odd){background:rgba(15,23,42,0.025);}' +
      '.st-kind{font-family:inherit;font-size:9.5px;opacity:.68;}' +
      '.st-dt{opacity:.58;text-align:right;}' +
      '.st-tps{position:relative;text-align:right;padding-right:4px;overflow:hidden;border-radius:3px;}' +
      '.st-bar{position:absolute;left:0;top:1px;bottom:1px;background:rgba(148,163,184,0.18);z-index:-1;border-radius:3px;}' +
      '.wrap.light .st-bar{background:rgba(15,23,42,0.08);}' +
      '.warn-tip{margin-top:8px;padding:6px 8px;border-radius:7px;background:rgba(245,158,11,0.10);border:1px solid rgba(245,158,11,0.24);color:#fbbf24;font-size:10px;display:flex;align-items:center;justify-content:space-between;gap:8px;}' +
      '.wrap.light .warn-tip{background:rgba(245,158,11,0.10);color:#92400e;}' +
      '.compact-btn{border:1px solid rgba(245,158,11,0.45);background:rgba(245,158,11,0.18);color:inherit;border-radius:5px;padding:2px 7px;font-size:10px;font-weight:600;cursor:pointer;white-space:nowrap;flex-shrink:0;transition:background .12s;}' +
      '.compact-btn:hover{background:rgba(245,158,11,0.30);}' +
      '.empty{font-size:11px;opacity:.65;padding:8px 4px 2px;text-align:center;}' +
      '</style>' +
      '<div class="wrap dark" id="agy-wrap">' +
        '<div class="pop" id="agy-pop"><div id="agy-pop-body"></div></div>' +
        '<button type="button" class="chip" id="agy-chip" title="点击展开上下文与 Token 遥测详情">' +
          '<svg width="14" height="14" viewBox="0 0 16 16" style="transform:rotate(-90deg);flex-shrink:0">' +
            '<circle cx="8" cy="8" r="5.5" fill="none" stroke="rgba(148,163,184,0.26)" stroke-width="2"></circle>' +
            '<circle id="agy-ring" cx="8" cy="8" r="5.5" fill="none" stroke="#3b82f6" stroke-width="2" stroke-linecap="round"></circle>' +
          '</svg>' +
          '<span id="agy-ctx-text" class="chip-num">0 / 1M</span>' +
          '<span class="mid-dot">·</span>' +
          '<span class="dot idle" id="agy-spd-dot"></span>' +
          '<span id="agy-spd-text" class="chip-num">- tok/s</span>' +
        '</button>' +
      '</div>';

    hudWrap = hudShadow.getElementById('agy-wrap');
    var chipEl = hudShadow.getElementById('agy-chip');
    var popEl = hudShadow.getElementById('agy-pop');

    if (chipEl) {
      chipEl.addEventListener('click', function(e) {
        e.stopPropagation();
        hudExpanded = !hudExpanded;
        renderHud();
        if (hudExpanded) positionPopover();
      });
    }

    if (popEl) {
      popEl.addEventListener('click', function(e) {
        e.stopPropagation();
        var zhBtn = e.target && e.target.closest ? e.target.closest('.zh-toggle') : null;
        if (zhBtn) {
          toggleZhEnabled();
          return;
        }
        var cmpBtn = e.target && e.target.closest ? e.target.closest('.compact-btn') : null;
        if (cmpBtn) {
          fillCompactCommand();
          return;
        }
        var btn = e.target && e.target.closest ? e.target.closest('.cap-btn') : null;
        if (btn) {
          var v = parseInt(btn.getAttribute('data-cap') || '0', 10);
          hudCapOverride = isNaN(v) ? 0 : v;
          saveStorage('__agy_hud_ctx_cap', hudCapOverride);
          renderHud();
        }
      });
    }
  }

  function toggleZhEnabled() {
    try {
      window.__agy_zh_enabled = !(window.__agy_zh_enabled !== false);
    } catch (e) {}
    renderHud();
  }

  function fillCompactCommand() {
    try {
      var input = null;
      var cands = document.querySelectorAll('[aria-label="Message input"], [aria-label="消息输入框"], [role="textbox"][contenteditable="true"], [contenteditable="true"], textarea');
      for (var i = 0; i < cands.length; i++) {
        var el = cands[i];
        if (!el || !el.isConnected) continue;
        if (el.closest && el.closest('#__agy_metrics_host, aside, nav, [role="dialog"]')) continue;
        var r = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
        if (r && r.width > 80 && r.height > 12) {
          input = el;
          break;
        }
      }
      if (!input) return;
      hudExpanded = false;
      renderHud();
      input.focus();
      if (input.tagName === 'TEXTAREA' || input.tagName === 'INPUT') {
        var setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value');
        if (setter && setter.set) setter.set.call(input, '/compact');
        else input.value = '/compact';
        input.dispatchEvent(new Event('input', { bubbles: true }));
      } else {
        var sel = window.getSelection ? window.getSelection() : null;
        if (sel && document.createRange) {
          var range = document.createRange();
          range.selectNodeContents(input);
          sel.removeAllRanges();
          sel.addRange(range);
        }
        var ok = false;
        try { ok = document.execCommand && document.execCommand('insertText', false, '/compact'); } catch (e) {}
        if (!ok) {
          input.textContent = '/compact';
          input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: '/compact' }));
        }
      }
    } catch (e) {}
  }

  function ensureHudMounted() {
    if (!hudEnabled) {
      if (hudHost && hudHost.parentNode) hudHost.parentNode.removeChild(hudHost);
      hudExpanded = false;
      return;
    }
    var anchor = findModelSelectorAnchor();
    if (!anchor) {
      if (hudHost && hudHost.parentNode) hudHost.parentNode.removeChild(hudHost);
      hudExpanded = false;
      hudAnchorBtn = null;
      return;
    }

    if (!hudHost) createHudHost();
    if (!hudHost) return;

    var row = anchor.row;
    var afterNode = anchor.afterNode;
    hudAnchorBtn = anchor.modelBtn;

    if (hudHost.parentNode !== row || hudHost.previousElementSibling !== afterNode) {
      row.insertBefore(hudHost, afterNode.nextSibling);
    }

    try {
      if (window.getComputedStyle) {
        var rowStyle = window.getComputedStyle(row);
        if (rowStyle && rowStyle.justifyContent === 'space-between') {
          hudHost.style.marginRight = 'auto';
        } else {
          hudHost.style.marginRight = '';
        }
        var btnStyle = window.getComputedStyle(hudAnchorBtn);
        if (btnStyle && hudWrap) {
          if (btnStyle.fontFamily) {
            hudHost.style.fontFamily = btnStyle.fontFamily;
            hudWrap.style.fontFamily = btnStyle.fontFamily;
          }
          if (btnStyle.fontSize) hudWrap.style.fontSize = btnStyle.fontSize;
          if (btnStyle.fontWeight) hudWrap.style.fontWeight = btnStyle.fontWeight;
          if (btnStyle.color) hudWrap.style.setProperty('--agy-hud-fg', btnStyle.color);
        }
      }
    } catch (e) {}

    renderHud();
  }

  function scheduleHudRemount() {
    if (hudRemountTimer) return;
    hudRemountTimer = setTimeout(function() {
      hudRemountTimer = null;
      ensureHudMounted();
    }, 80);
  }

  function renderHud() {
    if (!hudShadow || !hudWrap) return;
    if (!hudEnabled || !hudHost || !hudHost.isConnected) {
      hudWrap.style.display = 'none';
      return;
    }
    hudWrap.style.display = 'inline-flex';

    var isDark = detectDarkTheme();
    hudWrap.classList.toggle('dark', isDark);
    hudWrap.classList.toggle('light', !isDark);
    hudWrap.classList.toggle('expanded', hudExpanded);

    syncRouteChange();
    var m = pickActiveConversation(hudPayload);
    var anchorModel = inferAnchorModelInfo();
    var ring = hudShadow.getElementById('agy-ring');
    var ctxText = hudShadow.getElementById('agy-ctx-text');
    var spdText = hudShadow.getElementById('agy-spd-text');
    var spdDot = hudShadow.getElementById('agy-spd-dot');
    var popBody = hudShadow.getElementById('agy-pop-body');

    var autoLimit = m ? (m.contextLimit || anchorModel.limit) : anchorModel.limit;
    var limit = hudCapOverride > 0 ? hudCapOverride : autoLimit;
    var ctxTok = m ? (m.contextTokens || 0) : 0;
    var pct = limit > 0 ? Math.min(100, (100 * ctxTok / limit)) : 0;
    var pctStr = pct.toFixed(1) + '%';

    if (ring) {
      var circ = 2 * Math.PI * 5.5;
      var offset = circ * (1 - Math.min(1, pct / 100));
      var ringStroke = !m ? '#94a3b8' : (pct >= 85 ? '#ef4444' : (pct >= 65 ? '#f59e0b' : (isDark ? '#60a5fa' : '#2563eb')));
      ring.setAttribute('stroke', ringStroke);
      ring.setAttribute('stroke-dasharray', circ.toFixed(2));
      ring.setAttribute('stroke-dashoffset', offset.toFixed(2));
    }

    var narrow = false;
    try {
      if (hudHost.parentElement && hudHost.parentElement.getBoundingClientRect) {
        var pr = hudHost.parentElement.getBoundingClientRect();
        if (pr.width > 0 && pr.width < 520) narrow = true;
      }
    } catch (e) {}

    if (ctxText) {
      if (!m) {
        ctxText.textContent = narrow ? '0' : ('0 / ' + formatTok(limit));
      } else if (narrow) {
        ctxText.textContent = formatTok(ctxTok) + ' (' + Math.round(pct) + '%)';
      } else {
        ctxText.textContent = formatTok(ctxTok) + ' / ' + formatTok(limit) + ' (' + pctStr + ')';
      }
    }

    if (spdDot && spdText) {
      var isWorking = detectDomWorking() || !!(m && m.isWorking);
      if (isWorking) {
        spdDot.className = 'dot live';
        var liveTps = (streamActive && streamLastTps)
          ? streamLastTps
          : (m && m.currentTurnTps !== null && m.currentTurnTps !== undefined
            ? (narrow ? Math.round(m.currentTurnTps) : m.currentTurnTps)
            : null);
        spdText.textContent = liveTps !== null ? ('工作中 ' + liveTps + ' tok/s') : '工作中';
      } else {
        spdDot.className = 'dot idle';
        var idleTps = m ? (m.lastTurnAvgTps || m.avgTps || m.lastTps) : null;
        if (idleTps !== null && idleTps !== undefined) {
          spdText.textContent = narrow ? ('上次平均 ' + Math.round(idleTps) + ' tok/s') : ('上次平均 ' + idleTps + ' tok/s');
        } else {
          spdText.textContent = !m ? '新对话' : '暂无测速';
        }
      }
    }

    if (hudExpanded) positionPopover();

    if (popBody && hudExpanded) {
      var capBtns = [0, 1000000, 512000, 256000, 200000].map(function(val) {
        var label = val === 0 ? '自动' : formatTok(val);
        var active = hudCapOverride === val ? ' active' : '';
        return '<button class="cap-btn' + active + '" data-cap="' + val + '">' + label + '</button>';
      }).join('');

      var zhOn = window.__agy_zh_enabled !== false;
      var zhBtnHtml = '<button type="button" class="zh-toggle' + (zhOn ? '' : ' en') + '" title="点击原地切换中/英文（免刷新）">' + (zhOn ? '中' : 'EN') + '</button>';

      if (!m) {
        var emptyWorking = detectDomWorking();
        var emptyBadge = emptyWorking
          ? '<span class="status-tag live"><i class="dot live"></i>实时运行</span>'
          : '<span class="status-tag">新对话</span>';
        popBody.innerHTML =
          '<div class="pop-head">' +
            '<div class="head-left"><span class="model-name">' + anchorModel.name + '</span></div>' +
            '<div class="head-right">' + zhBtnHtml + emptyBadge + '</div>' +
          '</div>' +
          '<div class="ctx-block">' +
            '<div class="ctx-row">' +
              '<span class="ctx-title">上下文窗口</span>' +
              '<span class="mono">0 / ' + formatExact(limit) + ' (0.0%)</span>' +
            '</div>' +
            '<div class="stack-track"></div>' +
            '<div class="cap-row"><span class="cap-label">窗口基准</span><div class="cap-group">' + capBtns + '</div></div>' +
            '<div class="cap-note">注：Gemini 底层支持 1M，Antigravity 常在 256K~512K 触发自动压缩</div>' +
          '</div>' +
          '<div class="empty">当前为新对话，发送消息并完成首次模型调用后自动展示遥测数据。</div>';
        return;
      }

      var cacheTok = m.cacheTokens || 0;
      var inputTok = m.inputTokens || 0;
      var outTok = m.lastOutputTokens || 0;
      var cachePct = limit > 0 ? Math.min(100, (100 * cacheTok / limit)) : 0;
      var inputPct = limit > 0 ? Math.min(Math.max(0, 100 - cachePct), (100 * inputTok / limit)) : 0;
      var outPct = limit > 0 ? Math.min(Math.max(0, 100 - cachePct - inputPct), (100 * outTok / limit)) : 0;

      var steps = (m.recentSteps || []).slice().reverse();
      var maxStepTps = 1;
      for (var i = 0; i < steps.length; i++) {
        if (steps[i].tps && steps[i].tps > maxStepTps) maxStepTps = steps[i].tps;
      }
      var rowsHtml = steps.map(function(s) {
        var w = s.tps ? Math.max(8, Math.round(100 * s.tps / maxStepTps)) : 0;
        var tpsLabel = s.tps ? (s.tps + ' t/s') : '-';
        var dtLabel = s.dt !== null && s.dt !== undefined ? (s.dt + 's') : '-';
        var shortId = (s.label || ('#' + s.step)).replace(/^第/, 'R').replace(/轮·/, '·');
        return '<div class="step-row" title="上下文 ' + formatExact(s.ctx) + '｜输出 ' + formatExact(s.out) + '｜耗时 ' + dtLabel + '">' +
          '<span class="st-id">' + shortId + '</span>' +
          '<span class="st-kind">' + (s.kind || '调用') + '</span>' +
          '<span class="st-ctx">' + formatTok(s.ctx) + '</span>' +
          '<span class="st-out">' + formatTok(s.out) + '</span>' +
          '<span class="st-dt">' + dtLabel + '</span>' +
          '<span class="st-tps">' + tpsLabel + '<i class="st-bar" style="width:' + w + '%"></i></span>' +
        '</div>';
      }).join('');

      var tipHtml = pct >= 50
        ? '<div class="warn-tip"><span>上下文已过半，可压缩历史释放窗口</span><button type="button" class="compact-btn">填入 /compact</button></div>'
        : '';

      var popWorking = detectDomWorking() || !!m.isWorking;
      var statusBadge = popWorking
        ? '<span class="status-tag live"><i class="dot live"></i>实时运行</span>'
        : '<span class="status-tag">空闲</span>';
      var lastAvgVal = m.lastTurnAvgTps !== null && m.lastTurnAvgTps !== undefined ? (m.lastTurnAvgTps + ' tok/s') : '-';
      var lastStepVal = m.lastTps !== null && m.lastTps !== undefined ? (m.lastTps + ' tok/s') : '-';
      var allAvgVal = m.avgTps !== null && m.avgTps !== undefined ? (m.avgTps + ' tok/s') : '-';
      var convShort = m.convShort ? '<span class="conv-tag">#' + m.convShort + '</span>' : '';
      var turnDurSuffix = (m.lastTurnDurationSec !== null && m.lastTurnDurationSec !== undefined)
        ? (' · ' + (m.lastTurnDurationSec >= 60 ? (Math.floor(m.lastTurnDurationSec / 60) + 'm' + Math.round(m.lastTurnDurationSec % 60) + 's') : (m.lastTurnDurationSec + 's')))
        : '';

      popBody.innerHTML =
        '<div class="pop-head">' +
          '<div class="head-left"><span class="model-name">' + (m.modelName || '未知模型') + '</span>' + convShort + '</div>' +
          '<div class="head-right">' + zhBtnHtml + statusBadge + '</div>' +
        '</div>' +
        '<div class="ctx-block">' +
          '<div class="ctx-row">' +
            '<span class="ctx-title">上下文窗口</span>' +
            '<span class="mono">' + formatExact(ctxTok) + ' / ' + formatExact(limit) + ' (' + pctStr + ')</span>' +
          '</div>' +
          '<div class="stack-track' + (pct >= 85 ? ' danger' : (pct >= 65 ? ' warn' : '')) + '">' +
            (cachePct > 0 ? '<div class="stack-seg seg-cache" style="width:' + cachePct.toFixed(2) + '%" title="缓存读取: ' + formatExact(cacheTok) + '"></div>' : '') +
            (inputPct > 0 ? '<div class="stack-seg seg-input" style="width:' + inputPct.toFixed(2) + '%" title="新增输入: ' + formatExact(inputTok) + '"></div>' : '') +
            (outPct > 0 ? '<div class="stack-seg seg-out" style="width:' + outPct.toFixed(2) + '%" title="最近输出: ' + formatExact(outTok) + '"></div>' : '') +
          '</div>' +
          '<div class="legend-row">' +
            '<span class="leg"><i class="leg-dot" style="background:' + (isDark ? '#3b82f6' : '#2563eb') + '"></i>缓存 ' + formatTok(cacheTok) + ' (' + m.cacheHitRate + '%)</span>' +
            '<span class="leg"><i class="leg-dot" style="background:' + (isDark ? 'rgba(96,165,250,0.55)' : '#93c5fd') + '"></i>新增 ' + formatTok(inputTok) + '</span>' +
            '<span class="leg"><i class="leg-dot" style="background:' + (isDark ? 'rgba(148,163,184,0.6)' : '#cbd5e1') + '"></i>输出 ' + formatTok(outTok) + '</span>' +
          '</div>' +
          '<div class="cap-row"><span class="cap-label">窗口基准</span><div class="cap-group">' + capBtns + '</div></div>' +
          '<div class="cap-note">注：Gemini 底层支持 1M，Antigravity 常在 256K~512K 触发自动压缩</div>' +
        '</div>' +
        '<div class="tele-grid">' +
          '<div class="tele-cell"><div class="k">' + (popWorking ? '本轮平均' : '上次平均') + ' (' + (m.lastTurnSteps || 1) + ' 步' + turnDurSuffix + ')</div><div class="v mono">' + lastAvgVal + '</div></div>' +
          '<div class="tele-cell"><div class="k">最近单步速度</div><div class="v mono">' + lastStepVal + '</div></div>' +
          '<div class="tele-cell"><div class="k">全会话总平均</div><div class="v mono">' + allAvgVal + '</div></div>' +
          '<div class="tele-cell"><div class="k">本轮 / 累计产出</div><div class="v mono">' + formatTok(m.lastTurnOutputTokens) + ' / ' + formatTok(m.totalOutputTokens) + '</div></div>' +
        '</div>' +
        (rowsHtml ? '<div class="steps-title"><span>调用轨迹明细</span><span class="steps-hint">悬停查看精确字节</span></div><div class="step-head"><span>步次</span><span>类型</span><span>上下文</span><span>输出</span><span style="text-align:right">耗时</span><span style="text-align:right">速度</span></div><div class="steps-list">' + rowsHtml + '</div>' : '') +
        tipHtml;
    }
  }

  function initHud() {
    if (!document.body) return;
    ensureHudMounted();

    if (!hudDocClickHandler) {
      hudDocClickHandler = function(e) {
        if (!hudExpanded) return;
        if (hudHost && (e.target === hudHost || (hudHost.contains && hudHost.contains(e.target)))) return;
        hudExpanded = false;
        renderHud();
      };
      document.addEventListener('click', hudDocClickHandler);
    }

    if (!hudKeyHandler) {
      hudKeyHandler = function(e) {
        if (e && e.key === 'Escape' && hudExpanded) {
          hudExpanded = false;
          renderHud();
        }
      };
      document.addEventListener('keydown', hudKeyHandler);
    }

    if (!hudVisHandler && typeof document.addEventListener === 'function') {
      hudVisHandler = function() {
        if (!document.hidden) {
          domDirty = true;
          ensureHudMounted();
        }
      };
      document.addEventListener('visibilitychange', hudVisHandler);
    }

    if (!hudResizeHandler && window.addEventListener) {
      hudResizeHandler = function() {
        if (hudExpanded) positionPopover();
        renderHud();
      };
      window.addEventListener('resize', hudResizeHandler);
    }

    if (typeof setInterval === 'function') {
      if (!hudThemeTimer) hudThemeTimer = setInterval(renderHud, 3000);
      if (!hudStreamTimer) hudStreamTimer = setInterval(updateStreamSpeed, 250);
    }
  }

  function destroyHud() {
    try { if (hudThemeTimer) { clearInterval(hudThemeTimer); hudThemeTimer = null; } } catch (e) {}
    try { if (hudStreamTimer) { clearInterval(hudStreamTimer); hudStreamTimer = null; } } catch (e) {}
    try { if (hudRemountTimer) { clearTimeout(hudRemountTimer); hudRemountTimer = null; } } catch (e) {}
    try { if (hudResizeHandler && window.removeEventListener) { window.removeEventListener('resize', hudResizeHandler); hudResizeHandler = null; } } catch (e) {}
    try { if (hudDocClickHandler) { document.removeEventListener('click', hudDocClickHandler); hudDocClickHandler = null; } } catch (e) {}
    try { if (hudKeyHandler) { document.removeEventListener('keydown', hudKeyHandler); hudKeyHandler = null; } } catch (e) {}
    try { if (hudVisHandler) { document.removeEventListener('visibilitychange', hudVisHandler); hudVisHandler = null; } } catch (e) {}
    try {
      var old = document.getElementById('__agy_metrics_host');
      if (old && old.parentNode) old.parentNode.removeChild(old);
    } catch (e) {}
    hudHost = null;
    hudShadow = null;
    hudWrap = null;
    hudAnchorBtn = null;
    hudExpanded = false;
  }

  window.__agy_update_metrics = function(payload) {
    hudPayload = payload || null;
    var active = pickActiveConversation(hudPayload);
    if (active && active.updatedAt > streamBaselineUpdatedAt) {
      streamBaselineUpdatedAt = active.updatedAt || 0;
      tempIdleTps = null;
    }
    ensureHudMounted();
    renderHud();
  };

  try {
    Object.defineProperty(window, '__agy_hud_enabled', {
      configurable: true,
      get: function() { return hudEnabled; },
      set: function(v) { hudEnabled = !!v; ensureHudMounted(); renderHud(); }
    });
  } catch (e) {}

  // ====== MutationObserver（关键修补：新增 attributes + attributeFilter）======
  var observer = new MutationObserver(function(mutations) {
    if (isRestoring) return;
    domDirty = true;
    var sawChildList = false;
    for (var m = 0; m < mutations.length; m++) {
      if (mutations[m].type === 'characterData' || mutations[m].type === 'childList') {
        recordStreamingMutation(mutations[m]);
      }
      if (mutations[m].type === 'childList') sawChildList = true;
    }
    if (sawChildList && hudEnabled && (!hudHost || !hudHost.isConnected)) {
      scheduleHudRemount();
    }
    if (window.__agy_zh_enabled === false) return;
    for (var i = 0; i < mutations.length; i++) {
      var mu = mutations[i];
      if (mu.type === 'attributes') {
        processElementAttrs(mu.target);
      } else if (mu.type === 'characterData') {
        processTextNode(mu.target);
      } else if (mu.type === 'childList') {
        for (var j = 0; j < mu.addedNodes.length; j++) enqueue(mu.addedNodes[j]);
        // 节点被移除时清掉记录，避免 Weak 引用语义混乱（可选，WeakSet 会自动回收）
      }
    }
    syncStats();
  });

  function observe() {
    if (!document.body) return false;
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: TR_ATTRS
    });
    return true;
  }

  // ====== 启动时登记「当前页面现有文本」作为原文 ======
  // 这样中英切换不必依赖是哪个引擎实例翻的：只要本实例启动时见到过原文，
  // 关闭翻译时就能还原回去（节点被 React 更新时以最新一次为准）。
  function captureExisting() {
    if (!document.body || !textOrigins) return;
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
    var n;
    while ((n = walker.nextNode())) {
      var t = n.textContent || '';
      if (!t.trim() || !/[A-Za-z]/.test(t)) continue;   // 只登记含英文的节点
      if (!textOrigins.has(n)) textOrigins.set(n, t);
    }
  }

  // ====== 还原英文（中英切换用）======
  function restoreEnglish() {
    var n = 0;
    if (textOrigins) {
      textOrigins.forEach(function (orig, node) {
        try {
          if (node && node.isConnected && node.textContent !== orig) { node.textContent = orig; n++; }
        } catch (e) {}
      });
      textOrigins.clear();
    }
    if (attrOrigins) {
      attrOrigins.forEach(function (map, el) {
        try {
          if (!el || !el.isConnected) return;
          for (var k in map) { el.setAttribute(k, map[k]); n++; }
        } catch (e) {}
      });
      attrOrigins.clear();
    }
    return n;
  }

  // ====== 开关：可随时中英切换 ======
  try {
    Object.defineProperty(window, '__agy_zh_enabled', {
      configurable: true,
      get: function() { return window.__agy_zh_flag !== false; },
      set: function(v) {
        var nextFlag = !!v;
        if (window.__agy_zh_flag === nextFlag) return;
        window.__agy_zh_flag = nextFlag;
        if (nextFlag) {
          // 重置节点缓存，保证还原英文后再切回中文时所有节点都能重新翻译
          textSeen = HAS_WEAKSET ? new WeakSet() : null;
          textOrigin = HAS_WEAKMAP ? new WeakMap() : null;
          attrOrigin = HAS_WEAKMAP ? new WeakMap() : null;
          domDirty = true;
          observe();
          if (document.body) walkAndTranslate(document.body);
        } else {
          // 保持 observer 运行以维持 HUD 流式测速与重挂，仅屏蔽还原期间的自触发
          isRestoring = true;
          try {
            restored = restoreEnglish();
          } finally {
            isRestoring = false;
          }
          textSeen = HAS_WEAKSET ? new WeakSet() : null;
          textOrigin = HAS_WEAKMAP ? new WeakMap() : null;
          attrOrigin = HAS_WEAKMAP ? new WeakMap() : null;
        }
        syncStats();
        renderHud();
      }
    });
    window.__agy_zh_flag = true;
  } catch (e) { /* 环境不支持时退化为始终启用 */ }

  // 供热升级与外部调用：断开监听并还原英文
  window.__agy_zh_uninstall = function () {
    try { observer.disconnect(); } catch (e) {}
    try { if (rescanTimer) { clearInterval(rescanTimer); rescanTimer = null; } } catch (e) {}
    destroyHud();
    return restoreEnglish();
  };

  // ====== 启动 ======
  var rescanTimer = null;

  // 定时全量重扫：保证设置面板等动态渲染的内容被翻译
  // MutationObserver 会捕获新节点，但 React 异步渲染可能导致节点被首次处理时
  // 文本还不完整，textOrigin 缓存了"无法翻译"的状态，此后不再重试。
  // 重扫逻辑：找到所有仍含英文且能匹配词典的文本节点，清除缓存后强制重新翻译。
  function rescan() {
    if (document.hidden) return;
    ensureHudMounted();
    if (window.__agy_zh_enabled === false) return;
    if (!document.body) return;
    var now = Date.now();
    // 仅在发生 DOM 变动后或超过 15 秒兜底周期时才执行全量 TreeWalker，空闲时零耗
    if (!domDirty && (now - lastRescanAt) < 15000) return;
    domDirty = false;
    lastRescanAt = now;
    var walker = document.createTreeWalker(
      document.body,
      NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT,
      null,
      false
    );
    var n, retried = 0;
    while ((n = walker.nextNode())) {
      if (n.nodeType === 1) {
        if (!SKIP_TAGS[n.tagName]) {
          var beforeAttr = touchedAttr;
          processElementAttrs(n);
          if (touchedAttr > beforeAttr) retried++;
        }
        continue;
      }
      var cur = n.textContent || '';
      // 只处理含英文的、非空的文本
      if (!cur.trim() || cur.trim().length < 2 || !/[a-zA-Z]/.test(cur)) continue;
      var parent = n.parentElement;
      if (isSkippableParent(parent)) continue;
      if (parent && parent.childNodes && parent.childNodes.length > 1) {
        if (tryTranslateSplitContainer(parent)) { retried++; continue; }
      }
      var trimmedCur = cur.trim();
      if (AMBIGUOUS_SHORT[trimmedCur] && ((parent && parent.childNodes && parent.childNodes.length > 1) || cur !== trimmedCur)) {
        continue;
      }
      // 对仍含英文的节点尝试翻译（清除旧缓存）
      var result = tryTranslate(cur);
      if (result === null) result = tryCompactTime(cur, parent);
      result = fixNewBadge(cur, trimmedCur, result, parent);
      if (result !== null && result !== cur) {
        if (textOrigins && !textOrigins.has(n)) textOrigins.set(n, cur);
        n.textContent = result;
        touchedText++;
        if (textOrigin) textOrigin.set(n, result);
        retried++;
      }
    }
    if (retried) syncStats();
  }

  function boot() {
    if (!document.body) { setTimeout(boot, 50); return; }
    observe();
    captureExisting();          // 先登记现状（英文原文），再开始翻译
    walkAndTranslate(document.body);
    var repaired = repairCjkSpacing();
    cjkFixed = repaired;
    initHud();
    syncStats();
    if (repaired) console.log('[汉化] 已修复旧引擎遗留的词间空格: ' + repaired + ' 处');
    // 启动定时重扫（每 1.5 秒），保证动态面板被翻译
    if (rescanTimer && typeof clearInterval === 'function') clearInterval(rescanTimer);
    if (typeof setInterval === 'function') rescanTimer = setInterval(rescan, 1500);
    console.log('[汉化] observer_plus 已激活｜词条 ' + dictKeys.length +
                '｜严格模式 ' + (!CFG.allowPartialPhrases) +
                '｜已监听属性重译｜定时重扫已启动');
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

})(%%DICT%%);
