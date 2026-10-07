# -*- coding: utf-8 -*-
"""
live_ui.py - 仪表盘的页面（单个内联字符串，零 CDN）

为什么不用 generate_html_report.py 那一套：它是 2119 行、报告形状、而且
从 Google Fonts 和 jsdelivr 拉字体和 Chart.js —— 离线打不开。监控页面必须
离线可用，所以字体走系统栈、图表手写 SVG、不发任何外部请求。

设计取向（来自对 crush-monitor-universal 的研读）：
  · 按 tick 打补丁，不做 token 流 —— 消息行按 key 复用，只重画变了的
  · 「待标注」是一等视觉状态，不是一个转不完的圈 —— 语义层不常驻，
    界面必须诚实地表达「这部分还没人看过」
  · 守护进程死了要显眼 —— 看心跳年龄，不看 phase（被 SIGKILL 的进程
    来不及把 phase 改成 stopped）
  · 长列表用 CSS content-visibility 而不是手搓虚拟滚动
"""

HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>她不一样 · 实时</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #161a21;
    --panel-2: #1c2129;
    --line: #262c36;
    --text: #e6e9ef;
    --dim: #8b93a1;
    --me: #2f7d5d;
    --me-b: #3d9c76;
    --them: #232a34;
    --ok: #3fa772;
    --warn: #d8a041;
    --down: #d2564f;
    --info: #5b8fd6;
    --accent: #7aa2f7;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; }
  body {
    background: var(--bg); color: var(--text);
    font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI",
          "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB", sans-serif;
    display: flex; flex-direction: column; height: 100%;
  }
  header {
    padding: 12px 18px; border-bottom: 1px solid var(--line);
    background: var(--panel); display: flex; align-items: center; gap: 16px;
    flex-wrap: wrap;
  }
  header h1 { font-size: 15px; margin: 0; font-weight: 600; letter-spacing: .02em; }
  header .sub { color: var(--dim); font-size: 12px; }
  .pills { display: flex; gap: 8px; margin-left: auto; flex-wrap: wrap; }
  .pill {
    font-size: 12px; padding: 3px 10px; border-radius: 999px;
    background: var(--panel-2); border: 1px solid var(--line); color: var(--dim);
    white-space: nowrap;
  }
  .pill b { color: var(--text); font-weight: 600; }
  .pill.ok   { border-color: #2c5f45; color: #7fd0a6; }
  .pill.warn { border-color: #6b5426; color: var(--warn); }
  .pill.down { border-color: #6b2f2c; color: #ef8b85; background: #241a1a; }
  .pill.info { border-color: #2b4a70; color: #8fb6e8; }

  #banner { display: flex; flex-direction: column; }
  .banner-row {
    padding: 9px 18px; font-size: 13px; border-bottom: 1px solid var(--line);
    display: flex; gap: 10px; align-items: baseline;
  }
  .banner-row .lbl { font-weight: 600; }
  .banner-row .det { color: var(--dim); font-size: 12px; }
  .banner-row.down { background: #2a1a1a; color: #ef8b85; }
  .banner-row.warn { background: #2a2418; color: var(--warn); }
  .banner-row.info { background: #18222e; color: #8fb6e8; }

  main { flex: 1; display: flex; min-height: 0; }
  #stream {
    flex: 1; overflow-y: auto; padding: 18px 20px 60px;
    scroll-behavior: smooth;
  }
  .day {
    text-align: center; color: var(--dim); font-size: 12px;
    margin: 22px 0 14px; letter-spacing: .06em;
  }
  .day:first-child { margin-top: 0; }
  .row {
    display: flex; margin: 3px 0; gap: 8px;
    content-visibility: auto; contain-intrinsic-size: 0 44px;
  }
  .row.me { justify-content: flex-end; }
  .row.sys { justify-content: center; }
  .meta {
    align-self: flex-end; font-size: 11px; color: var(--dim);
    padding: 0 2px 4px; white-space: nowrap; font-variant-numeric: tabular-nums;
  }
  .bubble {
    max-width: min(620px, 68%); padding: 8px 12px; border-radius: 10px;
    background: var(--them); border: 1px solid var(--line);
    white-space: pre-wrap; word-break: break-word;
  }
  .row.me .bubble { background: var(--me); border-color: var(--me-b); }
  .bubble.missing { color: var(--dim); font-style: italic; border-style: dashed; }
  .bubble .tag {
    display: inline-block; margin-top: 6px; font-size: 11px; color: var(--dim);
    border: 1px dashed var(--line); border-radius: 4px; padding: 0 5px;
  }
  .row.fresh .bubble { box-shadow: 0 0 0 2px rgba(122,162,247,.35); }
  .note { font-size: 11px; color: var(--warn); margin-top: 5px; }

  /* ---- 标注层 ---- */
  #anno-bar {
    display: flex; align-items: center; gap: 12px;
    padding: 8px 18px; border-bottom: 1px solid var(--line);
    background: var(--panel); font-size: 12.5px; flex-wrap: wrap;
  }
  #anno-bar .stat { color: var(--dim); }
  #anno-bar .stat b { color: var(--text); font-weight: 600; }
  #anno-bar .stat.pending b { color: var(--warn); }
  .filters { display: flex; gap: 6px; margin-left: auto; flex-wrap: wrap; }
  .fbtn {
    font-size: 12px; padding: 2px 10px; border-radius: 999px;
    background: var(--panel-2); border: 1px solid var(--line); color: var(--dim);
    cursor: pointer; line-height: 20px;
  }
  .fbtn.on { border-color: var(--accent); color: var(--text); background: #1d2838; }

  .anno {
    display: flex; flex-direction: column; gap: 3px; margin-top: 7px;
    padding-top: 7px; border-top: 1px dashed var(--line);
  }
  .anno.rows { gap: 5px; }
  .anno .state-tag {
    align-self: flex-start; font-size: 11px; padding: 1px 7px; border-radius: 4px;
    border: 1px solid var(--line); color: var(--dim);
  }
  .anno .state-tag.done { color: #7fd0a6; border-color: #2c5f45; }
  .anno .state-tag.incomplete { color: var(--warn); border-color: #6b5426; }
  .anno .state-tag.skipped { color: var(--dim); border-style: dashed; }
  .anno .state-tag.pending { color: #ef8b85; border-color: #6b2f2c; background: #241a1a; }
  .anno .state-tag.analyzing { color: #8fb6e8; border-color: #2b4a70; background: #18222e; }
  .anno .state-tag.stale { color: var(--warn); border-color: #6b5426; background: #2a2418; }
  /* 历史从未被标注过 —— 是事实，不是待办。别用警示色，否则整页飘红。 */
  .anno .state-tag.unannotated { color: #5c636e; border-color: #2a3038; border-style: dotted; }
  .anno .lab-row { display: flex; flex-wrap: wrap; gap: 4px 8px; }
  .anno .lab { font-size: 11.5px; color: var(--text); }
  .anno .lab .pct { color: var(--dim); font-variant-numeric: tabular-nums; }
  .anno .reason { font-size: 11px; color: var(--dim); }
  .anno .lab-row.q { font-size: 11px; color: var(--accent); }
  .prob-row { display: flex; align-items: center; gap: 6px; font-size: 11px; }
  .prob-row .pname { width: 4.5em; text-align: right; color: var(--dim); flex: none; }
  .prob-row .pbar { flex: 1; height: 5px; background: var(--panel-2); border-radius: 2px; overflow: hidden; }
  .prob-row .pbar i { display: block; height: 100%; background: var(--accent); }
  .prob-row .pnum { width: 3.4em; text-align: right; color: var(--dim); font-variant-numeric: tabular-nums; flex: none; }
  .expand { cursor: pointer; }

  /* 时间轴 */
  #moments { border-left: 1px solid var(--line); background: var(--panel); padding: 14px 16px; flex: none; width: 272px; overflow-y: auto; font-size: 12px; }
  #moments h2 { font-size: 11px; text-transform: uppercase; letter-spacing: .1em; color: var(--dim); margin: 0 0 10px; font-weight: 600; }
  .moment { border-left: 2px solid var(--accent); padding: 4px 0 4px 10px; margin: 0 0 12px; cursor: pointer; position: relative; }
  .moment .t { font-weight: 600; }
  .moment .k { color: var(--dim); font-size: 11px; margin: 1px 0; }
  .moment .w { color: var(--dim); font-size: 11.5px; }

  aside {
    width: 292px; border-left: 1px solid var(--line); background: var(--panel);
    padding: 16px; overflow-y: auto; font-size: 12.5px;
  }
  aside h2 {
    font-size: 11px; text-transform: uppercase; letter-spacing: .1em;
    color: var(--dim); margin: 20px 0 8px; font-weight: 600;
  }
  aside h2:first-child { margin-top: 0; }
  .kv { display: flex; justify-content: space-between; padding: 3px 0; gap: 10px; }
  .kv span:first-child { color: var(--dim); }
  .kv span:last-child { text-align: right; font-variant-numeric: tabular-nums; }
  .bar {
    height: 6px; background: var(--panel-2); border-radius: 3px;
    overflow: hidden; margin-top: 4px;
  }
  .bar i { display: block; height: 100%; background: var(--accent); }
  code { font-size: 11px; color: var(--dim); word-break: break-all; }
  .empty { color: var(--dim); padding: 40px; text-align: center; }
  #jump {
    position: fixed; right: 312px; bottom: 22px; padding: 7px 14px;
    border-radius: 999px; background: var(--accent); color: #0d1117;
    border: 0; font-weight: 600; cursor: pointer; display: none;
    box-shadow: 0 4px 14px rgba(0,0,0,.4);
  }
</style>
</head>
<body>
<header>
  <div>
    <h1 id="who">实时检测</h1>
    <div class="sub" id="sub">连接中…</div>
  </div>
  <div class="pills" id="pills"></div>
</header>
<div id="banner"></div>
<div id="anno-bar">
  <span class="stat" id="anno-count">待标注 <b>…</b></span>
  <span class="stat" id="anno-last">上次标注 <b>…</b></span>
  <span class="filters" id="filters"></span>
</div>

<main>
  <section id="stream"><div class="empty">加载中…</div></section>
  <nav id="moments"><h2>关键时机</h2><div id="moments-list"></div></nav>
  <aside>
    <h2>数据源</h2>
    <div id="src"></div>
    <h2>摄取</h2>
    <div id="ing"></div>
    <h2>盲区</h2>
    <div id="blind"></div>
  </aside>
</main>
<button id="jump">跳到最新</button>

<script>
"use strict";
var byKey = new Map();     // msg_key -> DOM 行，按 key 复用（打补丁，不重建）
var lastCount = -1;
var lastDay = "";
var following = true;
var known = new Set();     // 已见过的 key，用来给新到的消息加高亮
var annoByKey = new Map(); // msg_key -> 标注 body（per_message 条目）
var progressByRequest = new Map(); // request_id -> {state, per_message:{msg_key:state}}
var moments = [];          // key_moments 时间轴
var queuedKeys = new Set(); // 本批 inbox 队列里的 msg_key —— 只有这些是「待标注」待办
var filterMode = "all";    // all | pending | incomplete | analyzing

function esc(s) { return s; }  // 用 textContent，不做 HTML 拼接

function fmtTime(ts) {
  var d = new Date(ts * 1000);
  var p = function (n) { return (n < 10 ? "0" : "") + n; };
  return p(d.getHours()) + ":" + p(d.getMinutes());
}
function fmtDay(ts) {
  var d = new Date(ts * 1000);
  var p = function (n) { return (n < 10 ? "0" : "") + n; };
  return (d.getMonth() + 1) + "月" + d.getDate() + "日 周" + "日一二三四五六"[d.getDay()];
}
function age(sec) {
  if (sec == null) return "未知";
  sec = Math.floor(sec);
  if (sec < 60) return sec + " 秒前";
  if (sec < 3600) return Math.floor(sec / 60) + " 分钟前";
  if (sec < 86400) return Math.floor(sec / 3600) + " 小时前";
  return Math.floor(sec / 86400) + " 天前";
}
function kv(k, v) {
  var d = document.createElement("div");
  d.className = "kv";
  var a = document.createElement("span"); a.textContent = k;
  var b = document.createElement("span"); b.textContent = v;
  d.appendChild(a); d.appendChild(b);
  return d;
}

function pct(v) {
  if (v == null) return "—";
  var p = v * 100;
  if (p > 0 && p < 0.5) return "<1%";
  return Math.round(p) + "%";
}

// 取 probs 里概率最高的前 3 个，返回 [{name, p}, ...]（降序）
function topProbs(probs, n) {
  n = n || 3;
  if (!probs) return [];
  return Object.keys(probs)
    .map(function (k) { return { name: k, p: probs[k] }; })
    .sort(function (a, b) { return b.p - a.p; })
    .slice(0, n);
}

function annoStateOf(anno) {
  if (!anno) return "pending";
  // 缺 state 字段 = 形状不全，不能默认成「已完成」——诚实的地面歧是 pending
  return anno.state || "pending";
}

// progress 里某个 msg_key 的 state（跨所有 request 取最「活跃」的：analyzing 优先级最高）
function progressStateOf(key) {
  var best = null;
  progressByRequest.forEach(function (pr) {
    var pm = pr.per_message || {};
    var s = pm[key];
    if (!s) return;
    if (s === "analyzing") { best = "analyzing"; return; }
    if (best === null && (s === "pending" || s === "done" || s === "incomplete" || s === "skipped")) {
      best = s;
    }
  });
  return best;
}

// 合并后的最终态：annotation 是成品、优先级最高；否则用 progress。
// 返回 {state, anno} —— state 是给气泡的最终状态，anno 是成品标注 body（可能 null）。
function effStateOf(key) {
  var anno = annoByKey.get(key) || null;
  if (anno) {
    var s = anno.state || "pending";
    if (s !== "pending") return { state: s, anno: anno };
  }
  var ps = progressStateOf(key);
  if (ps === "analyzing") return { state: "analyzing", anno: null };
  // 有 progress 但没到 analyzing（比如 pending/claimed），仍算 analyzing 前的中态
  if (ps && ps !== "done" && ps !== "skipped" && ps !== "incomplete") {
    return { state: "analyzing", anno: null };
  }
  // 没成品、没进度。这里必须区分两件事：
  //   在本批队列里 → 「待标注」（红色，是待办）
  //   不在队列里   → 「未标注 · 历史」（灰色，不是待办）
  // 混成一个红色的后果是 1541 条历史全挂「待标注」，整个页面看起来像坏了。
  if (queuedKeys.has(key)) return { state: "pending", anno: null };
  return { state: "unannotated", anno: null };
}

function renderAnno(bubble, eff, isMe) {
  var anno = eff.anno;
  var state = eff.state;
  // 历史从未被标注过 —— 不挂任何标签。1541 条历史各挂一个灰标签只是噪音，
  // 而挂红的会让整页看起来像坏了。它既不是待办也不是错误，就是「没有这一层」。
  if (state === "unannotated") return null;
  var wrap = document.createElement("div");
  wrap.className = "anno";

  var tag = document.createElement("span");
  tag.className = "state-tag " + state;
  var labels = { done: "已标注", incomplete: "不完整", skipped: "跳过",
                 pending: "待标注", analyzing: "标注中", stale: "标注中断",
                 unannotated: "未标注 · 历史" };
  tag.textContent = labels[state] || state;
  wrap.appendChild(tag);

  if (state === "done" || state === "incomplete") {
    var ev = anno.emotion || {};
    if (ev.label) {
      var er = document.createElement("div");
      er.className = "lab-row";
      var el = document.createElement("span"); el.className = "lab";
      el.textContent = "情绪 " + ev.label;
      er.appendChild(el);
      if (ev.confidence != null) {
        var ec = document.createElement("span"); ec.className = "pct";
        ec.textContent = pct(ev.confidence);
        er.appendChild(ec);
      }
      wrap.appendChild(er);
    }
    var iv = anno.intent || {};
    if (iv.label) {
      var ir = document.createElement("div");
      ir.className = "lab-row";
      var il = document.createElement("span"); il.className = "lab";
      il.textContent = "意图 " + iv.label;
      ir.appendChild(il);
      if (iv.confidence != null) {
        var ic = document.createElement("span"); ic.className = "pct";
        ic.textContent = pct(iv.confidence);
        ir.appendChild(ic);
      }
      wrap.appendChild(ir);
    }
    var tops = topProbs(anno.probs, 3);
    if (tops.length) {
      var pr = document.createElement("div");
      pr.className = "lab-row q";
      pr.textContent = tops.map(function (t) { return t.name + " " + pct(t.p); }).join(" · ");
      wrap.appendChild(pr);
    }
    if (anno.reason) {
      var rr = document.createElement("div");
      rr.className = "reason"; rr.textContent = anno.reason;
      wrap.appendChild(rr);
    }
    // 展开：完整概率条形分布
    var detail = document.createElement("div");
    detail.style.display = "none";
    var all = topProbs(anno.probs, 100);
    all.forEach(function (t) {
      var row = document.createElement("div");
      row.className = "prob-row";
      var nm = document.createElement("span"); nm.className = "pname"; nm.textContent = t.name;
      var bar = document.createElement("span"); bar.className = "pbar";
      var fill = document.createElement("i"); fill.style.width = Math.min(100, t.p * 100) + "%";
      bar.appendChild(fill);
      var num = document.createElement("span"); num.className = "pnum"; num.textContent = pct(t.p);
      row.appendChild(nm); row.appendChild(bar); row.appendChild(num);
      detail.appendChild(row);
    });
    wrap.appendChild(detail);
    wrap.className += " expand";
    wrap.addEventListener("click", function (ev) {
      ev.stopPropagation();
      detail.style.display = detail.style.display === "none" ? "block" : "none";
    });
  }
  bubble.appendChild(wrap);
  return wrap;
}

function renderMessages(msgs) {
  var stream = document.getElementById("stream");
  if (!msgs.length) {
    stream.innerHTML = "";
    var e = document.createElement("div");
    e.className = "empty"; e.textContent = "还没有消息";
    stream.appendChild(e);
    return;
  }
  if (lastCount < 0) { stream.innerHTML = ""; }
  var freshKeys = [];
  msgs.forEach(function (m) {
    if (byKey.has(m.key)) return;
    var eff = effStateOf(m.key);
    var state = eff.state;
    if (filterMode === "pending" && state !== "pending") return;
    if (filterMode === "incomplete" && state !== "incomplete") return;
    if (filterMode === "analyzing" && state !== "analyzing") return;
    var day = fmtDay(m.timestamp);
    if (day !== lastDay) {
      var dh = document.createElement("div");
      dh.className = "day"; dh.textContent = day;
      stream.appendChild(dh); lastDay = day;
    }
    var row = document.createElement("div");
    row.className = "row " + (m.sender === "me" ? "me" : (m.sender === "unknown" ? "sys" : "them"));
    if (known.has(m.key) === false && known.size > 0) { row.className += " fresh"; freshKeys.push(m.key); }
    var b = document.createElement("div");
    b.className = "bubble" + (m.text_missing ? " missing" : "");
    b.textContent = m.text_missing && !m.content ? "[内容不可得]" : m.content;
    if (m.text_missing) {
      var t = document.createElement("span");
      t.className = "tag"; t.textContent = "非文本 · 无内容";
      b.appendChild(document.createElement("br")); b.appendChild(t);
    }
    if (m.has_quote) {
      var q = document.createElement("div");
      q.className = "tag";
      q.textContent = m.quote_source === "refermsg" ? "引用原文" : "引用（推断）";
      b.appendChild(document.createElement("br")); b.appendChild(q);
    }
    if (m.note) {
      var n = document.createElement("div");
      n.className = "note"; n.textContent = m.note;
      b.appendChild(n);
    }
    // 语义标注：本批待标注的挂红标签；历史未标注的不挂（见 renderAnno）
    renderAnno(b, eff, m.sender === "me");
    var mt = document.createElement("div");
    mt.className = "meta"; mt.textContent = fmtTime(m.timestamp);
    row.appendChild(b); row.appendChild(mt);
    row.dataset.state = state;      // 给 refreshStates 判断要不要重画
    stream.appendChild(row);
    byKey.set(m.key, row);
    known.add(m.key);
  });
  lastCount = msgs.length;
  if (freshKeys.length) {
    setTimeout(function () { freshKeys.forEach(function (k) {
      var r = byKey.get(k); if (r) r.classList.remove("fresh");
    }); }, 2600);
  }
}

function setFilter(mode) {
  filterMode = mode;
  // 重建按钮状态
  document.querySelectorAll(".fbtn").forEach(function (b) {
    b.classList.toggle("on", b.getAttribute("data-mode") === mode);
  });
  // 重渲染：清掉 byKey 缓存，重建全量
  byKey = new Map();
  lastDay = "";
  lastCount = -1;
  pullMessages();
}

function renderFilters() {
  var el = document.getElementById("filters");
  el.innerHTML = "";
  [["all", "全部"], ["pending", "待标注"], ["analyzing", "标注中"], ["incomplete", "未完成"]].forEach(function (p) {
    var mode = p[0], label = p[1];
    var b = document.createElement("button");
    b.className = "fbtn" + (filterMode === mode ? " on" : "");
    b.setAttribute("data-mode", mode);
    b.textContent = label;
    b.addEventListener("click", function () { setFilter(mode); });
    el.appendChild(b);
  });
}

// 状态标签的刷新。必须和「建行」分开：行只在计数变化时建一次、之后缓存在
// byKey 里，而 queuedKeys / annoByKey / progress 都可能在建行之后才到齐
// （启动序列是 pullMessages → pullAnnotations → pull，queuedKeys 最后才填）。
// 不单独刷新的话，那几条「待标注」会永远停在首渲染时的样子。
// 靠 row.dataset.state 比对，没变就是空转，所以每轮调用也不贵。
function refreshStates() {
  byKey.forEach(function (row, key) {
    var eff = effStateOf(key);
    if (row.dataset.state === eff.state) return;
    row.dataset.state = eff.state;
    var bubble = row.querySelector(".bubble");
    if (!bubble) return;
    var old = bubble.querySelector(".anno");
    if (old) old.remove();
    renderAnno(bubble, eff, row.classList.contains("me"));
  });
}

function renderAnnoBar(s) {
  var a = s.annotations || {};
  queuedKeys = new Set(a.queued_keys || []);
  refreshStates();
  var pend = a.pending || 0;
  var analyzing = a.analyzing || 0;
  var staleRuns = a.stale_runs || 0;
  document.getElementById("anno-count").innerHTML =
    "待标注 <b>" + pend + "</b>" +
    (analyzing ? " · 正在标注 <b>" + analyzing + "</b>" : "") +
    " · 已标注 <b>" + (a.total_annotated || 0) + "</b>" +
    " · 请求 <b>" + (a.requests || 0) + "</b>";
  document.getElementById("anno-last").innerHTML =
    "上次标注 <b>" + (a.last_annotated_human || "从未") + "</b>" +
    (staleRuns ? " · <span style='color:var(--warn)'>" + staleRuns + " 个标注中断</span>" : "");
  renderFilters();
}

function renderMoments() {
  var el = document.getElementById("moments-list");
  el.innerHTML = "";
  if (!moments.length) {
    var e = document.createElement("div");
    e.className = "empty"; e.textContent = "还没有关键时机";
    el.appendChild(e);
    document.getElementById("moments").style.display = "none";
  } else {
    document.getElementById("moments").style.display = "";
    moments.forEach(function (m) {
      var d = document.createElement("div");
      d.className = "moment";
      var t = document.createElement("div"); t.className = "t"; t.textContent = m.title || "(无标题)";
      d.appendChild(t);
      if (m.kind) { var k = document.createElement("div"); k.className = "k"; k.textContent = m.kind; d.appendChild(k); }
      if (m.why) { var w = document.createElement("div"); w.className = "w"; w.textContent = m.why; d.appendChild(w); }
      d.addEventListener("click", function () { jumpTo(m.at_msg_key); });
      el.appendChild(d);
    });
  }
}

function jumpTo(key) {
  var row = byKey.get(key);
  var stream = document.getElementById("stream");
  if (row) { row.scrollIntoView({ block: "center" }); row.classList.add("fresh");
    setTimeout(function () { row.classList.remove("fresh"); }, 2600); }
}

function renderSnapshot(s) {
  document.getElementById("who").textContent =
    (s.contact && s.contact.display ? s.contact.display : "实时检测") + " · 实时";
  var lc = s.counts || {};
  document.getElementById("sub").textContent =
    "共 " + (lc.total || 0) + " 条（历史 " + (lc.bundle_messages || 0) +
    " + 实时 " + (lc.live_tail || 0) + "）· 快照 " + fmtTime(s.generated_at);

  var pills = document.getElementById("pills");
  pills.innerHTML = "";
  var d = s.daemon || {};
  var pd = document.createElement("div");
  pd.className = "pill " + (d.alive ? "ok" : "down");
  pd.innerHTML = "守护进程 <b>" + (d.alive ? "在跑" : "无心跳") + "</b>";
  pills.appendChild(pd);

  var lm = s.last_message || {};
  var pl = document.createElement("div");
  pl.className = "pill";
  pl.innerHTML = "最新消息 <b>" + (lm.human || "—") + "</b>";
  pills.appendChild(pl);

  var pe = document.createElement("div");
  pe.className = "pill";
  pe.innerHTML = "数据年龄 <b>" + age(lm.age_seconds) + "</b>";
  pills.appendChild(pe);

  var banner = document.getElementById("banner");
  banner.innerHTML = "";
  (s.health || []).forEach(function (h) {
    var row = document.createElement("div");
    row.className = "banner-row " + (h.level || "info");
    var a = document.createElement("span"); a.className = "lbl"; a.textContent = h.label;
    var b = document.createElement("span"); b.className = "det"; b.textContent = h.detail || "";
    row.appendChild(a); row.appendChild(b);
    banner.appendChild(row);
  });

  var src = document.getElementById("src"); src.innerHTML = "";
  src.appendChild(kv("联系人", (s.contact || {}).display || "—"));
  src.appendChild(kv("条数", String((s.counts || {}).total || 0)));
  src.appendChild(kv("最新一条来自", (s.last_message || {}).origin === "live" ? "实时尾巴" : "历史导出"));
  src.appendChild(kv("最新一条发出方", (s.last_message || {}).sender === "me" ? "你" : "对方"));

  var ing = document.getElementById("ing"); ing.innerHTML = "";
  var w = (s.ingest || {}).watchdog || {};
  ing.appendChild(kv("游标", (s.ingest || {}).cursor_human || "—"));
  ing.appendChild(kv("状态", (s.ingest || {}).health || "—"));
  ing.appendChild(kv("FTS 分片", String((w.tables_found || []).length)));
  var dr = (s.ingest || {}).decrypt || {};
  ing.appendChild(kv("解密模式", dr.mode || "—"));
  ing.appendChild(kv("WAL 补帧", String(dr.wal_frames == null ? 0 : dr.wal_frames)));
  ing.appendChild(kv("连续空 tick", String(w.consecutive_empty || 0)));
  var idn = (s.ingest || {}).identity || {};
  ing.appendChild(kv("锚点命中", String(idn.anchor_hits == null ? "—" : idn.anchor_hits)));
  ing.appendChild(kv("心跳", (s.daemon || {}).last_tick_human || "—"));

  var blind = document.getElementById("blind"); blind.innerHTML = "";
  var bs = s.blind_spots || {};
  blind.appendChild(kv("尾巴中无内容", String(bs.text_missing_in_tail || 0)));
  var nt = document.createElement("div");
  nt.className = "sub"; nt.style.color = "var(--dim)"; nt.style.marginTop = "6px";
  nt.textContent = bs.note || "";
  blind.appendChild(nt);

  renderAnnoBar(s);
}

async function pullMessages() {
  var r = await fetch("/api/messages");
  var j = await r.json();
  renderMessages(j.messages || []);
}

async function pullAnnotations() {
  try {
    var r = await fetch("/api/annotations");
    var j = await r.json();
    var changed = false;
    var prev = annoByKey;
    annoByKey = new Map();
    Object.keys(j.annotations || {}).forEach(function (k) {
      annoByKey.set(k, j.annotations[k]);
      if (!prev.has(k) || JSON.stringify(prev.get(k)) !== JSON.stringify(j.annotations[k])) changed = true;
    });
    if (prev.size !== annoByKey.size) changed = true;

    var prevProg = progressByRequest;
    progressByRequest = new Map();
    Object.keys(j.progress || {}).forEach(function (rid) {
      progressByRequest.set(rid, j.progress[rid]);
    });
    if (JSON.stringify([...progressByRequest]) !== JSON.stringify([...prevProg])) changed = true;

    moments = j.key_moments || [];
    renderMoments();
    if (changed) {
      // 标注或进度变化了：重建气泡，把新状态打进去
      byKey = new Map();
      lastDay = "";
      lastCount = -1;
      await pullMessages();
    }
  } catch (e) { /* 服务端可能还没装；忽略，快照仍会报错 */ }
}

async function pull() {
  try {
    var r = await fetch("/api/snapshot");
    var s = await r.json();
    renderSnapshot(s);
    var sig = JSON.stringify([(s.counts || {}).total, (s.last_message || {}).human]);
    if (sig !== pull.lastSig) {
      pull.lastSig = sig;
      // 计数变了才重取全量消息
      if ((s.counts || {}).total !== lastCount) await pullMessages();
    }
    var btn = document.getElementById("jump");
    btn.style.display = following ? "none" : "block";
  } catch (e) {
    var b = document.getElementById("banner");
    b.innerHTML = "";
    var row = document.createElement("div");
    row.className = "banner-row down";
    row.textContent = "连不上服务：" + e;
    b.appendChild(row);
  }
}

document.getElementById("stream").addEventListener("scroll", function (ev) {
  var el = ev.target;
  following = (el.scrollHeight - el.scrollTop - el.clientHeight) < 120;
});
document.getElementById("jump").addEventListener("click", function () {
  var el = document.getElementById("stream");
  el.scrollTop = el.scrollHeight; following = true;
});

pullMessages().then(function () { return pullAnnotations(); }).then(pull);
setInterval(pull, 2000);
setInterval(pullAnnotations, 5000);
</script>
</body>
</html>'''
