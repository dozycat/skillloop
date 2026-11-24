/* skillloop 操作台 — vanilla JS, hash routing.
   Rendering rule: every API string is untrusted (skill text, agent output, case input can contain
   HTML). All DOM is built through h(), which only ever sets textContent / attributes — no innerHTML. */
"use strict";

// ----------------------------------------------------------------------------- helpers
const SVGNS = "http://www.w3.org/2000/svg";

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "disabled" || k === "selected") el[k] = !!v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  add(el, kids);
  return el;
}
function add(el, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
function svg(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
  for (const kid of kids) el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}
const fmt = {
  num: (v, d = 3) => (typeof v === "number" && isFinite(v) ? v.toFixed(d) : "—"),
  pct: (v) => (typeof v === "number" && isFinite(v) ? (v * 100).toFixed(1) + "%" : "—"),
  delta: (v) => (typeof v === "number" && isFinite(v) ? (v >= 0 ? "+" : "") + v.toFixed(3) : "—"),
  time: (ts) => (ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "—"),
  clock: (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : ""),
  val: (v) => (v === null || v === undefined ? "—" : typeof v === "string" ? v : JSON.stringify(v, null, 1)),
  clip: (s, n = 140) => { s = fmt.val(s); return s.length > n ? s.slice(0, n) + "…" : s; },
};
const STATUS_CLASS = {
  succeeded: "ok", best: "ok", accepted: "info", fixed: "ok", done: "ok", ok: "ok",
  failed: "bad", rejected: "bad", regressed: "bad", error: "bad", timeout: "bad", interrupted: "warn",
  running: "info", queued: "info", cancelling: "warn", cancelled: "warn", pending_approval: "warn",
  pending: "warn", open: "warn", candidate: "", wontfix: "",
};
const STATUS_ZH = {
  succeeded: "成功", failed: "失败", running: "运行中", queued: "排队中", cancelling: "取消中", cancelled: "已取消",
  interrupted: "已中断", best: "最佳", accepted: "已接受", rejected: "已拒绝", candidate: "候选",
  pending_approval: "待人工批准", open: "未解决", fixed: "已修复", regressed: "回归", wontfix: "不修复",
  pending: "待标注", done: "已标注",
};
const KIND_ZH = { evolve: "进化", evaluate: "评测", devset: "开发集", bootstrap: "初版 Skill", outputs: "输出" };
const STAGE_ZH = {
  bootstrap: "生成初版 Skill", iteration: "迭代", "evaluate:train": "评测 · train", "evaluate:val": "评测 · val",
  "evaluate:test": "评测 · test", evaluate: "评测", analyze: "错题分析", merge: "补丁合并", test: "留出集验证",
  rules: "规则抽取", candidates: "候选生成", evolve: "场景演化", filter: "过滤", balance: "平衡", report: "报告",
};
const badge = (status) => h("span", { class: "badge " + (STATUS_CLASS[status] || "") }, STATUS_ZH[status] || status || "—");
const passBadge = (ok) => h("span", { class: "badge " + (ok ? "ok" : "bad") }, ok ? "通过" : "未通过");
const pre = (text, cls) => h("pre", { class: cls || "" }, text === "" || text == null ? "（空）" : fmt.val(text));
const empty = (...kids) => h("div", { class: "empty" }, ...kids);
const field = (label, control) => h("label", { class: "field" }, h("span", {}, label), control);

function toast(message, kind) {
  const el = h("div", { class: "toast " + (kind || "") }, message);
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "bad" ? 8000 : 3500);
}

// ----------------------------------------------------------------------------- API
let TOKEN = "";
try {
  const u = new URL(location.href);
  TOKEN = u.searchParams.get("token") || sessionStorage.getItem("skillloop-token") || "";
  if (u.searchParams.has("token")) {
    sessionStorage.setItem("skillloop-token", TOKEN);
    u.searchParams.delete("token");
    history.replaceState(null, "", u.pathname + u.search + u.hash);
  }
} catch (e) { /* storage may be unavailable; the token then lives for this page load only */ }

function withToken(url) {
  return TOKEN ? url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN) : url;
}
async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (TOKEN) opts.headers["Authorization"] = "Bearer " + TOKEN;
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch("/api" + path, opts);
  } catch (e) {
    document.getElementById("conn").textContent = "无法连接服务";
    throw new Error("无法连接操作台服务：" + e.message);
  }
  document.getElementById("conn").textContent = "";
  let data = null;
  try { data = await resp.json(); } catch (e) { /* non-JSON body */ }
  if (!resp.ok) {
    const msg = data && data.error ? data.error.message : "HTTP " + resp.status;
    const err = new Error(msg);
    err.status = resp.status;
    throw err;
  }
  return data;
}
const P = (name) => "/projects/" + encodeURIComponent(name);
const qs = (params) => {
  const s = Object.entries(params).filter(([, v]) => v !== "" && v != null)
    .map(([k, v]) => encodeURIComponent(k) + "=" + encodeURIComponent(v)).join("&");
  return s ? "?" + s : "";
};
/** Run an action, toast its failure, optionally toast success. Returns the result or undefined. */
async function act(fn, okMessage) {
  try {
    const r = await fn();
    if (okMessage) toast(okMessage, "ok");
    return r === undefined ? true : r;
  } catch (e) {
    toast(e.message, "bad");
    return undefined;
  }
}

// ----------------------------------------------------------------------------- routing
const main = document.getElementById("main");
let cleanup = [];   // disposers for the current view (EventSource, timers)
let renderSeq = 0;

const SECTIONS = [
  ["", "概览"], ["jobs", "任务"], ["versions", "版本"], ["evals", "评测"], ["errors", "错题库"],
  ["reviews", "Review"], ["artifacts", "产物"], ["config", "配置"],
];
const go = (hash) => { location.hash = hash; };
const link = (hash, ...kids) => h("a", { href: hash }, ...kids);
const pl = (name, rest) => "#/p/" + encodeURIComponent(name) + (rest ? "/" + rest : "");

function setChrome(project, section, info) {
  const crumbs = document.getElementById("crumbs");
  const nav = document.getElementById("sidenav");
  crumbs.replaceChildren();
  nav.replaceChildren();
  if (!project) return;
  add(crumbs, [h("span", { class: "sep" }, "/"), link(pl(project), project)]);
  for (const [key, label] of SECTIONS) {
    const a = h("a", { href: pl(project, key) }, h("span", {}, label));
    if (key === section) a.setAttribute("aria-current", "page");
    if (info) {
      const n = key === "reviews" ? info.pending_reviews : key === "jobs" ? (info.active_jobs || []).length
        : key === "errors" ? (info.errorbook || {}).open : 0;
      if (n) a.appendChild(h("span", { class: "badge warn" }, n));
    }
    nav.appendChild(a);
  }
}

async function route() {
  for (const fn of cleanup.splice(0)) { try { fn(); } catch (e) { /* ignore */ } }
  const seq = ++renderSeq;
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const view = h("div", {});
  let project = "", section = "";
  try {
    if (parts[0] !== "p" || !parts[1]) {
      setChrome("");
      await viewProjects(view);
    } else {
      project = parts[1];
      section = parts[2] || "";
      const info = await api("GET", P(project) + "/info");
      if (seq !== renderSeq) return;
      setChrome(project, section, info);
      const fn = VIEWS[section];
      if (!fn) throw new Error("未知页面：" + section);
      await fn(view, project, parts.slice(3), info);
    }
  } catch (e) {
    if (seq !== renderSeq) return;
    if (e.status === 401) {
      view.replaceChildren(tokenPrompt());
    } else {
      view.replaceChildren(h("div", { class: "callout bad" }, "加载失败：" + e.message),
        h("p", {}, link("#/", "返回项目列表")));
    }
  }
  if (seq !== renderSeq) return;
  main.replaceChildren(view);
}
window.addEventListener("hashchange", route);

function tokenPrompt() {
  const input = h("input", { type: "password", "aria-label": "访问令牌", placeholder: "访问令牌" });
  return h("div", { class: "panel" }, h("h2", {}, "需要访问令牌"),
    h("p", { class: "muted" }, "此操作台启用了令牌保护。请输入启动服务时设置的 token。"),
    h("form", { class: "row", onsubmit: (ev) => {
      ev.preventDefault();
      TOKEN = input.value.trim();
      try { sessionStorage.setItem("skillloop-token", TOKEN); } catch (e) { /* ignore */ }
      route();
    } }, input, h("button", { class: "primary", type: "submit" }, "进入")));
}

// ----------------------------------------------------------------------------- shared widgets
function bestScore(scores) {
  const s = (scores || {}).val || (scores || {}).all || (scores || {}).train;
  return s ? s.mean_score : null;
}
function scoreCells(scores) {
  return ["train", "val", "test"].map((sp) => {
    const s = (scores || {})[sp];
    return h("td", { class: "num", title: s ? "通过率 " + fmt.pct(s.pass_rate) + " · " + s.n_cases + " 例" : "未评测" },
      s ? fmt.num(s.mean_score) : "—");
  });
}
function trendChart(history) {
  const pts = (history || []).filter((r) => typeof r.best_score === "number");
  if (!pts.length) return empty("还没有迭代记录。点击「开始进化」后，这里会显示每轮的分数趋势。");
  const W = 520, H = 170, L = 34, R = 10, T = 10, B = 24;
  const n = pts.length;
  const x = (i) => L + (n === 1 ? (W - L - R) / 2 : (i * (W - L - R)) / (n - 1));
  const y = (v) => T + (1 - Math.max(0, Math.min(1, v))) * (H - T - B);
  const el = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": "每轮迭代的最佳 val 分数与 train 分数" });
  for (const v of [0, 0.5, 1]) {
    el.appendChild(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "axis" }));
    el.appendChild(svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, v.toFixed(1)));
  }
  const line = (key, cls, dot) => {
    const ok = pts.map((r, i) => [i, r[key]]).filter(([, v]) => typeof v === "number");
    if (!ok.length) return;
    el.appendChild(svg("polyline", { points: ok.map(([i, v]) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" "), class: cls }));
    for (const [i, v] of ok) {
      const c = svg("circle", { cx: x(i), cy: y(v), r: 3, class: dot });
      c.appendChild(svg("title", {}, `第 ${pts[i].iteration} 轮 ${key === "best_score" ? "最佳 val" : "train"}：${v.toFixed(3)}`));
      el.appendChild(c);
    }
  };
  line("train_score", "l2", "d2");
  line("best_score", "l1", "d1");
  const step = Math.max(1, Math.ceil(n / 10));
  pts.forEach((r, i) => { if (i % step === 0 || i === n - 1) el.appendChild(svg("text", { x: x(i), y: H - 6, "text-anchor": "middle" }, r.iteration)); });
  return h("div", {}, el, h("div", { class: "legend" }, h("span", {}, h("i", {}), "最佳版本 val 分数"),
    h("span", {}, h("i", { class: "l2" }), "父版本 train 分数"), h("span", {}, "横轴：迭代轮次")));
}
function diffView(text) {
  if (!text || !text.trim()) return empty("两个版本内容相同。");
  const box = h("pre", { class: "diff tall" });
  for (const line of text.split("\n")) {
    const cls = line.startsWith("+++") || line.startsWith("---") ? "file" : line.startsWith("@@") ? "hunk"
      : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "";
    box.appendChild(h("span", { class: cls }, line || " "));
  }
  return box;
}
function comparisonView(c) {
  const list = (label, ids, cls) => h("div", {}, h("h3", {}, `${label}（${(ids || []).length}）`),
    (ids || []).length ? h("div", { class: "row" }, ids.map((id) => h("span", { class: "badge " + cls }, id))) : h("span", { class: "muted small" }, "无"));
  return h("div", {},
    h("div", { class: "stats" },
      h("div", { class: "stat" }, h("b", {}, fmt.num(c.base_score) + " → " + fmt.num(c.cand_score)), h("span", {}, `${c.metric} · ${c.base_version} → ${c.cand_version}`)),
      h("div", { class: "stat" }, h("b", {}, fmt.delta(c.delta)), h("span", {}, `Δ（95% CI ${fmt.delta(c.ci_low)} ~ ${fmt.delta(c.ci_high)}）`)),
      h("div", { class: "stat" }, h("b", {}, fmt.num(c.p_value)), h("span", {}, "符号检验 p 值")),
      h("div", { class: "stat" }, h("b", {}, c.n_common), h("span", {}, `共同用例 · ${c.split || ""}`))),
    list("修复（未通过 → 通过）", c.fixed, "ok"), list("回归（通过 → 未通过）", c.regressed, "bad"),
    list("受保护用例回归", c.protected_regressions, "bad"),
    list("分数提升", c.improved, "info"), list("分数下降", c.worsened, "warn"),
    Object.keys(c.by_tag || {}).length ? h("div", {}, h("h3", {}, "按标签"), h("div", { class: "tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "标签"), h("th", { class: "num" }, "用例"), h("th", { class: "num" }, "基线"), h("th", { class: "num" }, "候选"), h("th", { class: "num" }, "Δ"))),
      h("tbody", {}, Object.entries(c.by_tag).map(([t, s]) => h("tr", {}, h("td", {}, t), h("td", { class: "num" }, s.n),
        h("td", { class: "num" }, fmt.num(s.base)), h("td", { class: "num" }, fmt.num(s.cand)), h("td", { class: "num" }, fmt.delta(s.delta)))))))) : null);
}
function versionSelect(versions, selected, extra) {
  return h("select", {}, (extra || []).map(([v, t]) => h("option", { value: v, selected: v === selected }, t)),
    versions.map((v) => h("option", { value: v.id, selected: v.id === selected }, `${v.id} · ${STATUS_ZH[v.status] || v.status}`)));
}
async function startJob(project, kind, params) {
  const r = await act(() => api("POST", P(project) + "/jobs", { kind, params }), "任务已提交");
  if (r) go(pl(project, "jobs/" + r.job_id));
}

// ----------------------------------------------------------------------------- 项目
async function viewProjects(view) {
  const { projects, single } = await api("GET", "/projects");
  add(view, [h("div", { class: "head" }, h("h1", {}, "项目"))]);
  if (!projects.length) {
    view.appendChild(empty("还没有项目。在下方填写项目名称与需求文档，创建第一个 Skill 进化项目。"));
  } else {
    view.appendChild(h("div", { class: "panel tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "项目"), h("th", {}, "最佳版本"), h("th", { class: "num" }, "最佳分数"),
        h("th", { class: "num" }, "用例"), h("th", { class: "num" }, "版本"), h("th", {}, "Agent"), h("th", {}, "LLM"), h("th", {}, "判分"))),
      h("tbody", {}, projects.map((p) => p.error
        ? h("tr", {}, h("td", {}, p.name), h("td", { colspan: 7, class: "muted" }, "无法打开：" + p.error))
        : h("tr", { class: "clickable", onclick: () => go(pl(p.name)) },
          h("td", {}, link(pl(p.name), p.name)), h("td", {}, p.best_version || "—"),
          h("td", { class: "num" }, fmt.num(bestScore(p.best_scores))), h("td", { class: "num" }, p.n_cases),
          h("td", { class: "num" }, p.n_versions), h("td", {}, p.agent), h("td", {}, p.llm), h("td", {}, p.judge)))))));
  }
  if (single) {
    view.appendChild(h("p", { class: "muted small" }, "当前以单项目模式运行（工作目录本身是一个项目）。在其上级目录启动操作台可管理多个项目。"));
    return;
  }
  const name = h("input", { type: "text", required: true, placeholder: "例如 invoice-extractor", pattern: "[A-Za-z0-9][A-Za-z0-9._\\-]{0,63}" });
  const req = h("textarea", { rows: 6, placeholder: "业务目标、范围、验收标准、约束……" });
  const skill = h("textarea", { rows: 5, placeholder: "---\nname: my-skill\ndescription: ...\n---\n\n（可留空：将根据需求文档自动生成初版）" });
  const cases = h("textarea", { rows: 5, placeholder: '每行一个 JSON：{"input": "...", "expected": "...", "tags": ["..."]}' });
  view.appendChild(h("form", { class: "panel", onsubmit: async (ev) => {
    ev.preventDefault();
    let records;
    try {
      records = cases.value.split("\n").map((l) => l.trim()).filter(Boolean).map((l, i) => {
        try { return JSON.parse(l); } catch (e) { throw new Error(`测试集第 ${i + 1} 行不是合法 JSON`); }
      });
    } catch (e) { toast(e.message, "bad"); return; }
    const body = { name: name.value.trim(), requirement: req.value, skill_md: skill.value };
    if (records.length) body.cases = records;
    const r = await act(() => api("POST", "/projects", body), "项目已创建");
    if (r) go(pl(body.name));
  } }, h("h2", {}, "新建项目"),
    field("项目名称（字母、数字、. _ -）", name), field("需求文档", req),
    field("初版 SKILL.md（可选）", skill), field("测试集 / 标准答案（可选，JSONL）", cases),
    h("button", { class: "primary", type: "submit" }, "创建项目")));
}

// ----------------------------------------------------------------------------- 概览
async function viewOverview(view, project, _rest, info) {
  const [health, history, versions] = await Promise.all([
    api("GET", P(project) + "/health").catch((e) => ({ _error: e.message })),
    api("GET", P(project) + "/history"), api("GET", P(project) + "/versions"),
  ]);
  add(view, [h("div", { class: "head" }, h("h1", {}, project), info.best_version ? h("span", { class: "badge ok" }, "最佳 " + info.best_version) : null)]);
  if (info.pending_reviews) view.appendChild(h("div", { class: "callout" }, `有 ${info.pending_reviews} 条结果等待人工标注。`, " ", link(pl(project, "reviews"), "去标注")));
  if ((info.pending_approval || []).length) view.appendChild(h("div", { class: "callout" }, "候选版本等待人工批准：",
    info.pending_approval.map((v) => [" ", link(pl(project, "versions/" + v), v)])));
  if ((info.active_jobs || []).length) view.appendChild(h("div", { class: "callout info" }, "有任务正在运行：",
    info.active_jobs.map((j) => [" ", link(pl(project, "jobs/" + j), j)])));
  for (const prob of (health.config_problems || [])) view.appendChild(h("div", { class: "callout bad" }, "配置问题：" + prob));

  const hc = (key) => {
    const s = health[key];
    if (!s) return h("span", { class: "badge" }, "未知");
    return h("span", { class: "badge " + (s.ok ? "ok" : "bad"), title: s.message }, s.ok ? "可用" : "不可用：" + fmt.clip(s.message, 60));
  };
  const sp = info.splits || {};
  const best = bestScore(info.best_scores);
  const inputs = h("div", { class: "panel" }, h("h2", {}, "输入与配置"), h("dl", { class: "kv" },
    h("dt", {}, "需求文档"), h("dd", {}, info.has_requirement ? h("span", { class: "badge ok" }, "已提供") : h("span", { class: "badge warn" }, "缺失"), " ", link(pl(project, "config"), "编辑")),
    h("dt", {}, "初版 Skill"), h("dd", {}, info.has_seed_skill ? h("span", { class: "badge ok" }, "已提供") : h("span", { class: "badge" }, "将自动生成")),
    h("dt", {}, "测试集"), h("dd", {}, info.n_cases ? `${info.n_cases} 例（train ${sp.train || 0} / val ${sp.val || 0} / test ${sp.test || 0}）`
      : h("span", { class: "badge warn" }, "为空 — 可用「生成开发集」创建")),
    h("dt", {}, "Agent 适配器"), h("dd", {}, info.agent, " ", hc("agent")),
    h("dt", {}, "LLM"), h("dd", {}, info.llm, " ", hc("llm")),
    h("dt", {}, "判分器"), h("dd", {}, info.judge),
    h("dt", {}, "目录"), h("dd", { class: "mono" }, info.root)));
  const eb = info.errorbook || {};
  const status = h("div", { class: "panel" }, h("h2", {}, "当前状态"), h("div", { class: "stats" },
    h("div", { class: "stat" }, h("b", {}, info.best_version || "—"), h("span", {}, "最佳版本")),
    h("div", { class: "stat" }, h("b", {}, fmt.num(best)), h("span", {}, "最佳 val 分数")),
    h("div", { class: "stat" }, h("b", {}, info.n_versions), h("span", {}, "版本数")),
    h("div", { class: "stat" }, h("b", {}, eb.open || 0), h("span", {}, "未解决错题")),
    h("div", { class: "stat" }, h("b", {}, eb.fixed || 0), h("span", {}, "已修复错题"))),
    h("h3", {}, "分数趋势"), trendChart(history.history));

  const iters = h("input", { type: "number", min: 1, max: 100, placeholder: "默认", "aria-label": "最大迭代轮数" });
  const evVer = versionSelect(versions.versions, "", [["", "最佳版本"]]);
  const evSplit = h("select", { "aria-label": "数据划分" }, [["", "全部用例"], ["train", "train"], ["val", "val"], ["test", "test"]].map(([v, t]) => h("option", { value: v }, t)));
  const merge = h("input", { type: "checkbox" });
  const actions = h("div", { class: "panel" }, h("h2", {}, "操作"),
    h("div", { class: "action" }, h("div", { class: "row" },
      h("button", { class: "primary", disabled: !info.n_cases, onclick: () => startJob(project, "evolve", { max_iterations: iters.value || null }) }, "开始进化"),
      h("label", {}, "最大轮数", iters)),
      h("p", { class: "muted small" }, info.n_cases ? "基线评测 → 错题分析 → 生成候选 → 版本比较 → 闸门通过后提升最佳版本。" : "测试集为空，请先添加用例或生成开发集。")),
    h("div", { class: "action" }, h("div", { class: "row" },
      h("button", { disabled: !info.n_versions || !info.n_cases, onclick: () => startJob(project, "evaluate", { version: evVer.value, split: evSplit.value }) }, "运行评测"), evVer, evSplit),
      !info.n_versions ? h("p", { class: "muted small" }, "还没有版本：先「开始进化」或在「版本」页导入 SKILL.md。") : null),
    h("div", { class: "action" }, h("div", { class: "row" },
      h("button", { disabled: !info.has_requirement, onclick: () => startJob(project, "devset", { merge: merge.checked }) }, "生成开发集"),
      h("label", {}, merge, "合并进测试集")),
      h("p", { class: "muted small" }, "规则抽取 → 候选生成 → 场景演化 → 过滤 → 平衡 → 报告。")),
    h("div", { class: "action" }, h("div", { class: "row" },
      h("button", { onclick: () => startJob(project, "outputs", {}) }, "生成输出"),
      h("span", { class: "muted small" }, "最佳 SKILL.md、Review 标注表、错题库、版本比较报告、指标摘要。"))));
  view.appendChild(h("div", { class: "grid" }, status, actions));
  view.appendChild(inputs);
}

// ----------------------------------------------------------------------------- 任务
async function viewJobs(view, project, rest) {
  if (rest[0]) return viewJob(view, project, rest[0]);
  const { jobs } = await api("GET", P(project) + "/jobs");
  add(view, [h("div", { class: "head" }, h("h1", {}, "任务"))]);
  if (!jobs.length) { view.appendChild(empty("还没有任务。到「概览」页点击「开始进化」或「运行评测」。")); return; }
  view.appendChild(h("div", { class: "panel tablewrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "任务"), h("th", {}, "类型"), h("th", {}, "状态"), h("th", {}, "进度"), h("th", {}, "创建时间"), h("th", {}, "更新时间"))),
    h("tbody", {}, jobs.map((j) => h("tr", { class: "clickable", onclick: () => go(pl(project, "jobs/" + j.id)) },
      h("td", { class: "mono" }, link(pl(project, "jobs/" + j.id), j.id)), h("td", {}, KIND_ZH[j.kind] || j.kind), h("td", {}, badge(j.status)),
      h("td", { class: "clip" }, j.error ? j.error : fmt.clip((j.progress || {}).message || "", 80)),
      h("td", { class: "nowrap" }, fmt.time(j.created_at)), h("td", { class: "nowrap" }, fmt.time(j.updated_at))))))));
}

async function viewJob(view, project, jobId) {
  let job = await api("GET", P(project) + "/jobs/" + encodeURIComponent(jobId));
  const FINAL = ["succeeded", "failed", "cancelled", "interrupted"];
  const statusEl = h("span", {});
  const stageEl = h("div", { class: "small" });
  const msgEl = h("div", { class: "muted small" });
  const bar = h("div", {});
  const barWrap = h("div", { class: "progress", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": 100 }, bar);
  const logEl = h("div", { class: "log", tabindex: 0, "aria-label": "任务日志" });
  const resultEl = h("div", {});
  const follow = h("input", { type: "checkbox", checked: true });
  const cancelBtn = h("button", { class: "danger", onclick: async () => {
    await act(() => api("POST", P(project) + "/jobs/" + encodeURIComponent(jobId) + "/cancel", {}), "已请求取消");
  } }, "取消任务");

  const showProgress = (p) => {
    if (!p || !p.stage) return;
    stageEl.textContent = "阶段：" + (STAGE_ZH[p.stage] || p.stage) + (p.total ? `（${p.current}/${p.total}）` : "");
    msgEl.textContent = p.message || "";
    if (p.total) {
      const pc = Math.max(0, Math.min(100, (100 * p.current) / p.total));
      barWrap.classList.remove("indet");
      bar.style.width = pc.toFixed(1) + "%";
      barWrap.setAttribute("aria-valuenow", pc.toFixed(0));
    } else if (!FINAL.includes(job.status)) {
      barWrap.classList.add("indet");
      barWrap.removeAttribute("aria-valuenow");
    }
  };
  const showJob = (j) => {
    job = j;
    statusEl.replaceChildren(badge(j.status));
    cancelBtn.disabled = FINAL.includes(j.status) || j.status === "cancelling";
    showProgress(j.progress);
    if (FINAL.includes(j.status)) {
      barWrap.classList.remove("indet");
      if (j.status === "succeeded") bar.style.width = "100%";
      resultEl.replaceChildren(
        j.error ? h("div", { class: "callout bad" }, j.error) : null,
        j.result != null ? h("div", {}, h("h3", {}, "结果"), jobResult(project, j)) : null);
    }
  };
  const appendEvent = (ev) => {
    if (ev.type === "progress") { showProgress(ev.data); if (ev.data && ev.data.case_id === undefined && !ev.message) return; }
    const cls = ev.type === "audit" ? "audit" : ev.type === "job" ? "job" : ev.level;
    const extra = ev.type === "job" && ev.data && ev.data.error ? " — " + ev.data.error : "";
    const tag = ev.type === "audit" ? "[审计] " : ev.type === "job" ? "[任务] " : "";
    logEl.appendChild(h("div", { class: cls }, h("span", { class: "t" }, fmt.clock(ev.ts) + " "), tag + ev.message + extra));
    while (logEl.childElementCount > 3000) logEl.firstElementChild.remove();
    if (follow.checked) logEl.scrollTop = logEl.scrollHeight;
  };

  add(view, [h("div", { class: "head" }, h("h1", {}, (KIND_ZH[job.kind] || job.kind) + "任务"), h("span", { class: "mono muted" }, job.id), statusEl,
    h("span", { class: "spacer" }), cancelBtn, link(pl(project, "jobs"), "全部任务")),
  h("div", { class: "panel" }, h("div", { class: "row" }, stageEl, h("span", { class: "spacer" }),
    h("span", { class: "muted small" }, "参数：" + JSON.stringify(job.params || {}))), barWrap, msgEl),
  h("div", { class: "panel" }, h("div", { class: "row" }, h("h2", {}, "日志"), h("span", { class: "spacer" }), h("label", { class: "small" }, follow, "跟随最新")), logEl),
  resultEl]);
  showJob(job);

  // Live stream (SSE, resumable by seq); falls back to polling if EventSource is unavailable or fails early.
  let last = 0, closed = false, es = null, timer = null;
  const finish = async () => {
    closed = true;
    if (es) es.close();
    if (timer) clearTimeout(timer);
    try { showJob(await api("GET", P(project) + "/jobs/" + encodeURIComponent(jobId))); } catch (e) { /* ignore */ }
  };
  const poll = async () => {
    if (closed) return;
    try {
      const r = await api("GET", P(project) + "/jobs/" + encodeURIComponent(jobId) + "/poll" + qs({ after: last }));
      for (const ev of r.events) { last = ev.seq; appendEvent(ev); }
      showJob(r.job);
      if (r.final && !r.events.length) { closed = true; return; }
    } catch (e) { /* transient; retry */ }
    timer = setTimeout(poll, 1000);
  };
  const startSSE = () => {
    es = new EventSource(withToken("/api" + P(project) + "/jobs/" + encodeURIComponent(jobId) + "/events" + qs({ after: last })));
    const onEv = (m) => { try { const ev = JSON.parse(m.data); last = ev.seq; appendEvent(ev); if (ev.type === "job") showJob(Object.assign({}, job, { status: ev.message === "started" ? "running" : job.status })); } catch (e) { /* skip */ } };
    for (const t of ["log", "progress", "job", "audit"]) es.addEventListener(t, onEv);
    es.addEventListener("end", finish);
    es.onerror = () => {
      if (closed) return;
      if (es.readyState === EventSource.CLOSED) { es = null; poll(); }  // server refused: fall back
    };
  };
  if (window.EventSource) startSSE(); else poll();
  cleanup.push(() => { closed = true; if (es) es.close(); if (timer) clearTimeout(timer); });
}

function jobResult(project, j) {
  const r = j.result;
  if (j.kind === "evolve" && r && typeof r === "object") {
    const b = (r.baseline_scores || {}).val, c = (r.best_scores || {}).val, bt = (r.baseline_scores || {}).test, ct = (r.best_scores || {}).test;
    return h("div", { class: "panel" }, h("div", { class: "stats" },
      h("div", { class: "stat" }, h("b", {}, link(pl(project, "versions/" + r.best_version), r.best_version || "—")), h("span", {}, "最佳版本")),
      h("div", { class: "stat" }, h("b", {}, fmt.num(b && b.mean_score) + " → " + fmt.num(c && c.mean_score)), h("span", {}, "val 分数（基线 → 最佳）")),
      h("div", { class: "stat" }, h("b", {}, fmt.num(bt && bt.mean_score) + " → " + fmt.num(ct && ct.mean_score)), h("span", {}, "留出 test 分数")),
      h("div", { class: "stat" }, h("b", {}, r.iterations), h("span", {}, "迭代轮数")),
      h("div", { class: "stat" }, h("b", {}, (r.promotions || []).length), h("span", {}, "提升次数"))),
    h("p", {}, "停止原因：" + (r.stop_reason || "—")),
    (r.pending_approval || []).length ? h("div", { class: "callout" }, "待人工批准：" + r.pending_approval.join("、")) : null,
    h("p", {}, link(pl(project, "versions"), "查看版本"), " · ", link(pl(project, "errors"), "查看错题库"), " · ", link(pl(project, "artifacts"), "查看产物")));
  }
  if (j.kind === "evaluate" && r && r.eval_id) {
    return h("div", { class: "panel" }, h("div", { class: "stats" },
      h("div", { class: "stat" }, h("b", {}, fmt.num(r.mean_score)), h("span", {}, "平均分")),
      h("div", { class: "stat" }, h("b", {}, fmt.pct(r.pass_rate)), h("span", {}, "通过率")),
      h("div", { class: "stat" }, h("b", {}, r.n_cases), h("span", {}, "用例"))),
    h("p", {}, link(pl(project, "evals/" + r.eval_id), "查看评测详情")));
  }
  if (j.kind === "devset" && r && r.report_md) {
    return h("div", {}, h("p", {}, `规则 ${r.n_rules} 条 · 保留用例 ${r.n_cases} 例 · 丢弃 ${r.n_dropped} 例` + (r.merged ? " · 已合并进测试集" : "")), pre(r.report_md, "tall"));
  }
  return pre(r);
}

// ----------------------------------------------------------------------------- 版本
async function viewVersions(view, project, rest) {
  const { versions, best } = await api("GET", P(project) + "/versions");
  if (rest[0]) return viewVersion(view, project, rest[0], versions, best);
  add(view, [h("div", { class: "head" }, h("h1", {}, "版本"))]);
  if (!versions.length) {
    view.appendChild(empty("还没有版本。「开始进化」会登记初版 Skill；也可以在下方直接导入一份 SKILL.md。"));
  } else {
    view.appendChild(h("div", { class: "panel tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "版本"), h("th", {}, "父版本"), h("th", {}, "来源"), h("th", {}, "状态"), h("th", { class: "num" }, "轮次"),
        h("th", { class: "num" }, "train"), h("th", { class: "num" }, "val"), h("th", { class: "num" }, "test"), h("th", {}, "变更摘要"))),
      h("tbody", {}, versions.slice().reverse().map((v) => h("tr", { class: "clickable", onclick: () => go(pl(project, "versions/" + v.id)) },
        h("td", {}, link(pl(project, "versions/" + v.id), v.id)), h("td", {}, v.parent || "—"), h("td", {}, v.origin), h("td", {}, badge(v.status)),
        h("td", { class: "num" }, v.iteration || "—"), scoreCells(v.scores), h("td", { class: "clip" }, fmt.clip(v.summary, 160))))))));
    const a = versionSelect(versions, versions[0].id), b = versionSelect(versions, best || versions[versions.length - 1].id);
    const split = h("select", { "aria-label": "数据划分" }, ["val", "train", "test"].map((s) => h("option", { value: s }, s)));
    const out = h("div", {});
    view.appendChild(h("div", { class: "panel" }, h("h2", {}, "版本比较"), h("div", { class: "row" }, h("label", {}, "基线", a), h("label", {}, "候选", b), split,
      h("button", { onclick: async () => {
        out.replaceChildren(h("p", { class: "muted" }, "比较中（缺少评测结果时会先运行评测）……"));
        const [c, d] = await Promise.all([
          act(() => api("POST", P(project) + "/compare", { base: a.value, cand: b.value, split: split.value })),
          act(() => api("GET", P(project) + "/diff" + qs({ a: a.value, b: b.value })))]);
        out.replaceChildren(c ? comparisonView(c) : null, d ? h("div", {}, h("h3", {}, "Skill 差异"), diffView(d.diff)) : null);
      } }, "比较")), out));
  }
  const text = h("textarea", { rows: 10, placeholder: "---\nname: my-skill\ndescription: 何时使用此 Skill\n---\n\n# 正文……" });
  const summary = h("input", { type: "text", placeholder: "变更说明", value: "manual edit" });
  view.appendChild(h("form", { class: "panel", onsubmit: async (ev) => {
    ev.preventDefault();
    const r = await act(() => api("POST", P(project) + "/versions/import", { skill_md: text.value, summary: summary.value }), "已导入为新版本");
    if (r) go(pl(project, "versions/" + r.id));
  } }, h("h2", {}, "导入手工编辑的 SKILL.md"), field("SKILL.md 内容", text), field("变更说明", summary), h("button", { type: "submit" }, "导入为新版本")));
}

async function viewVersion(view, project, vid, versions, best) {
  const v = await api("GET", P(project) + "/versions/" + encodeURIComponent(vid));
  const reload = () => route();
  const reason = () => window.prompt("原因（可选）", "") ;
  const btns = [];
  if (v.id !== best) {
    btns.push(h("button", { class: "primary", onclick: async () => {
      const r = reason(); if (r === null) return;
      if (await act(() => api("POST", P(project) + "/versions/" + encodeURIComponent(v.id) + "/promote", { reason: r || "manual promotion" }), "已提升为最佳版本")) reload();
    } }, v.status === "pending_approval" ? "批准并提升" : "提升为最佳"));
    if (v.status !== "rejected") btns.push(h("button", { class: "danger", onclick: async () => {
      const r = reason(); if (r === null) return;
      if (await act(() => api("POST", P(project) + "/versions/" + encodeURIComponent(v.id) + "/reject", { reason: r }), "已拒绝")) reload();
    } }, "拒绝"));
  }
  add(view, [h("div", { class: "head" }, h("h1", {}, v.id), badge(v.status), h("span", { class: "spacer" }), btns, link(pl(project, "versions"), "全部版本"))]);
  const gate = v.gate || {};
  view.appendChild(h("div", { class: "grid" },
    h("div", { class: "panel" }, h("h2", {}, "信息"), h("dl", { class: "kv" },
      h("dt", {}, "谱系"), h("dd", {}, (v.lineage || []).map((id, i) => [i ? " → " : "", id === v.id ? h("b", {}, id) : link(pl(project, "versions/" + id), id)])),
      h("dt", {}, "来源"), h("dd", {}, v.origin + (v.iteration ? ` · 第 ${v.iteration} 轮` : "")),
      h("dt", {}, "创建时间"), h("dd", {}, fmt.time(v.created_at)),
      h("dt", {}, "内容哈希"), h("dd", { class: "mono" }, v.content_hash),
      h("dt", {}, "变更摘要"), h("dd", {}, v.summary || "—")),
    h("h3", {}, "分数"), h("div", { class: "tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "划分"), h("th", { class: "num" }, "平均分"), h("th", { class: "num" }, "通过率"), h("th", { class: "num" }, "用例"), h("th", {}, "评测"))),
      h("tbody", {}, Object.keys(v.scores || {}).length ? Object.entries(v.scores).map(([sp, s]) => h("tr", {}, h("td", {}, sp),
        h("td", { class: "num" }, fmt.num(s.mean_score)), h("td", { class: "num" }, fmt.pct(s.pass_rate)), h("td", { class: "num" }, s.n_cases),
        h("td", {}, s.eval_id ? link(pl(project, "evals/" + s.eval_id), "详情") : "—")))
        : h("tr", {}, h("td", { colspan: 5, class: "muted" }, "尚未评测")))))),
    h("div", { class: "panel" }, h("h2", {}, "闸门"),
      (gate.checks || []).length ? h("div", { class: "tablewrap" }, h("table", {}, h("tbody", {}, gate.checks.map((c) => h("tr", {},
        h("td", { class: "nowrap" }, passBadge(c.passed)), h("td", { class: "nowrap" }, c.name), h("td", {}, c.detail))))))
        : h("p", { class: "muted" }, "该版本未经过闸门（初版或手工导入）。"),
      (gate.reasons || []).length ? h("div", {}, h("h3", {}, "结论"), h("ul", {}, gate.reasons.map((r) => h("li", {}, r)))) : null,
      gate.comparison && gate.comparison.n_common ? h("details", {}, h("summary", {}, "对比当时最佳版本的详情"), comparisonView(gate.comparison)) : null)));

  view.appendChild(h("div", { class: "panel" }, h("h2", {}, "SKILL.md"), pre(v.skill_md, "tall"),
    Object.entries(v.files || {}).map(([name, content]) => h("details", {}, h("summary", {}, name), pre(content)))));

  const others = versions.filter((x) => x.id !== v.id);
  if (others.length) {
    const sel = versionSelect(others, v.parent || others[0].id);
    const out = h("div", {});
    const show = async () => {
      const d = await act(() => api("GET", P(project) + "/diff" + qs({ a: sel.value, b: v.id })));
      out.replaceChildren(d ? diffView(d.diff) : null);
    };
    sel.addEventListener("change", show);
    view.appendChild(h("div", { class: "panel" }, h("div", { class: "row" }, h("h2", {}, "差异"), h("label", {}, "相对于", sel)), out));
    show();
  }
  if ((v.patches || []).length) view.appendChild(h("div", { class: "panel" }, h("h2", {}, "生成此版本的补丁"), h("details", {}, h("summary", {}, "查看补丁 JSON"), pre(v.patches, "tall"))));
}

// ----------------------------------------------------------------------------- 评测
async function viewEvals(view, project, rest) {
  if (rest[0]) return viewEval(view, project, rest[0]);
  const { evals } = await api("GET", P(project) + "/evals");
  add(view, [h("div", { class: "head" }, h("h1", {}, "评测"))]);
  if (!evals.length) { view.appendChild(empty("还没有评测记录。到「概览」页点击「运行评测」或「开始进化」。")); return; }
  view.appendChild(h("div", { class: "panel tablewrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "评测"), h("th", {}, "版本"), h("th", {}, "划分"), h("th", { class: "num" }, "平均分"), h("th", { class: "num" }, "通过率"),
      h("th", { class: "num" }, "用例"), h("th", { class: "num" }, "待复核"), h("th", { class: "num" }, "耗时"))),
    h("tbody", {}, evals.filter((e) => e && e.eval_id).map((e) => h("tr", { class: "clickable", onclick: () => go(pl(project, "evals/" + e.eval_id)) },
      h("td", { class: "mono" }, link(pl(project, "evals/" + e.eval_id), e.eval_id)), h("td", {}, e.skill_version || "—"), h("td", {}, e.label || e.split || "—"),
      h("td", { class: "num" }, fmt.num(e.mean_score)), h("td", { class: "num" }, fmt.pct(e.pass_rate)), h("td", { class: "num" }, e.n_cases),
      h("td", { class: "num" }, e.n_needs_review || 0), h("td", { class: "num" }, fmt.num(e.duration_s, 1) + "s")))))));
}

async function viewEval(view, project, evalId) {
  const [e, cs] = await Promise.all([api("GET", P(project) + "/evals/" + encodeURIComponent(evalId)), api("GET", P(project) + "/cases")]);
  const cases = Object.fromEntries(cs.cases.map((c) => [c.id, c]));
  const s = e.summary || {};
  add(view, [h("div", { class: "head" }, h("h1", {}, "评测"), h("span", { class: "mono muted" }, e.id), h("span", { class: "badge info" }, e.skill_version || "?"),
    h("span", { class: "badge" }, e.label || e.split || "all"), e.cancelled ? h("span", { class: "badge warn" }, "已取消（部分结果）") : null,
    h("span", { class: "spacer" }), h("a", { href: withToken("/api" + P(project) + "/review-sheet" + qs({ eval_id: e.id })) }, "导出 Review 标注表 (CSV)"), link(pl(project, "evals"), "全部评测"))]);
  view.appendChild(h("div", { class: "panel" }, h("div", { class: "stats" },
    h("div", { class: "stat" }, h("b", {}, fmt.num(s.mean_score)), h("span", {}, "平均分")),
    h("div", { class: "stat" }, h("b", {}, fmt.pct(s.pass_rate)), h("span", {}, "通过率")),
    h("div", { class: "stat" }, h("b", {}, fmt.num(s.score_lcb)), h("span", {}, "95% 置信下界")),
    h("div", { class: "stat" }, h("b", {}, s.n_cases), h("span", {}, `用例（${s.n_runs} 次运行）`)),
    h("div", { class: "stat" }, h("b", {}, s.n_needs_review || 0), h("span", {}, "待人工复核")))));
  const tags = Object.entries(e.by_tag || {});
  if (tags.length) view.appendChild(h("div", { class: "panel tablewrap" }, h("h2", {}, "按标签"), h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "标签"), h("th", { class: "num" }, "用例"), h("th", { class: "num" }, "平均分"), h("th", { class: "num" }, "通过率"))),
    h("tbody", {}, tags.map(([t, v]) => h("tr", {}, h("td", {}, t), h("td", { class: "num" }, v.n), h("td", { class: "num" }, fmt.num(v.mean_score)), h("td", { class: "num" }, fmt.pct(v.pass_rate))))))));

  const onlyFail = h("input", { type: "checkbox" });
  const body = h("tbody", {});
  const render = () => {
    body.replaceChildren();
    for (const r of e.results || []) {
      if (onlyFail.checked && r.judgment.passed) continue;
      const c = cases[r.case_id] || {};
      const t = r.trajectory || {};
      body.appendChild(h("tr", {}, h("td", { class: "mono nowrap" }, r.case_id + (r.trial ? ` #${r.trial}` : "")),
        h("td", { class: "nowrap" }, passBadge(r.judgment.passed), r.judgment.needs_review ? h("span", { class: "badge warn" }, "待复核") : null,
          r.judgment.source === "human" ? h("span", { class: "badge info" }, "人工") : null, t.status && t.status !== "ok" ? badge(t.status) : null),
        h("td", { class: "num" }, fmt.num(r.judgment.score, 2)),
        h("td", { class: "clip" }, r.judgment.reason || "—", (r.judgment.labels || []).map((l) => [" ", h("span", { class: "badge" }, l)]),
          h("details", {}, h("summary", {}, "输入 / 期望 / 输出 / 轨迹"),
            h("h3", {}, "输入"), pre(c.input), h("h3", {}, "期望"), pre(c.expected), c.rubric ? [h("h3", {}, "评分标准"), pre(c.rubric)] : null,
            h("h3", {}, "实际输出"), pre(t.output), t.error ? [h("h3", {}, "错误"), pre(t.error)] : null,
            (t.steps || []).length ? [h("h3", {}, `轨迹（${t.steps.length} 步）`), pre(t.steps.map((st) => `[${st.kind}${st.name ? ":" + st.name : ""}] ${st.content}`).join("\n"))] : null))));
    }
    if (!body.childElementCount) body.appendChild(h("tr", {}, h("td", { colspan: 4, class: "muted" }, onlyFail.checked ? "没有未通过的用例。" : "没有结果。")));
  };
  onlyFail.addEventListener("change", render);
  render();
  view.appendChild(h("div", { class: "panel tablewrap" }, h("div", { class: "row" }, h("h2", {}, "用例结果"), h("span", { class: "spacer" }), h("label", { class: "small" }, onlyFail, "只看未通过")),
    h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "用例"), h("th", {}, "结果"), h("th", { class: "num" }, "分数"), h("th", {}, "判分理由"))), body)));
}

// ----------------------------------------------------------------------------- 错题库
const CATEGORIES = ["missing_rule", "wrong_rule", "ambiguous_instruction", "format", "tool_misuse", "reasoning", "agent_error", "eval_issue", "unexplained"];
async function viewErrors(view, project) {
  const [{ errors }, stats] = await Promise.all([api("GET", P(project) + "/errors"), api("GET", P(project) + "/errors/stats")]);
  add(view, [h("div", { class: "head" }, h("h1", {}, "错题库"))]);
  if (!errors.length) { view.appendChild(empty("错题库为空。运行 train 划分的评测或「开始进化」后，失败用例会沉淀到这里。")); return; }
  const cats = Object.entries(stats.by_category || {});
  view.appendChild(h("div", { class: "grid" },
    h("div", { class: "panel" }, h("h2", {}, "状态"), h("div", { class: "stats" },
      [["total", "总计"], ["open", "未解决"], ["regressed", "回归"], ["fixed", "已修复"], ["wontfix", "不修复"]].map(([k, t]) => h("div", { class: "stat" }, h("b", {}, stats[k] || 0), h("span", {}, t))))),
    h("div", { class: "panel" }, h("h2", {}, "根因分类（未解决）"), cats.length ? h("div", { class: "row" }, cats.map(([c, n]) => h("span", { class: "badge" }, `${c} · ${n}`))) : h("span", { class: "muted" }, "没有未解决的错题。"))));
  const status = h("select", { "aria-label": "状态筛选" }, [["", "全部状态"], ["open", "未解决"], ["regressed", "回归"], ["fixed", "已修复"], ["wontfix", "不修复"]].map(([v, t]) => h("option", { value: v }, t)));
  const cat = h("select", { "aria-label": "根因筛选" }, h("option", { value: "" }, "全部根因"), [...new Set(errors.map((e) => e.category).filter(Boolean))].map((c) => h("option", { value: c }, c)));
  const search = h("input", { type: "search", placeholder: "搜索用例 / 输入 / 根因", "aria-label": "搜索" });
  const list = h("div", {});
  const render = () => {
    const q = search.value.trim().toLowerCase();
    const rows = errors.filter((e) => (!status.value || e.status === status.value) && (!cat.value || e.category === cat.value)
      && (!q || [e.case_id, e.input, e.root_cause, e.lesson].some((s) => String(s || "").toLowerCase().includes(q))));
    list.replaceChildren(...(rows.length ? rows.map((e) => errorCard(project, e)) : [empty("没有符合筛选条件的错题。")]));
  };
  for (const el of [status, cat]) el.addEventListener("change", render);
  search.addEventListener("input", render);
  view.appendChild(h("div", { class: "panel row" }, status, cat, search));
  view.appendChild(list);
  render();
}
function errorCard(project, e) {
  const cat = h("select", {}, h("option", { value: "" }, "（未分类）"), CATEGORIES.concat(CATEGORIES.includes(e.category) || !e.category ? [] : [e.category]).map((c) => h("option", { value: c, selected: c === e.category }, c)));
  const st = h("select", {}, ["open", "regressed", "fixed", "wontfix"].map((s) => h("option", { value: s, selected: s === e.status }, STATUS_ZH[s])));
  const rc = h("textarea", { rows: 2, value: e.root_cause || "" });
  const lesson = h("textarea", { rows: 2, value: e.lesson || "" });
  return h("div", { class: "panel" }, h("div", { class: "row" }, h("b", { class: "mono" }, e.case_id), badge(e.status),
    e.category ? h("span", { class: "badge" }, e.category) : null, (e.tags || []).map((t) => h("span", { class: "badge" }, t)),
    h("span", { class: "spacer" }), h("span", { class: "muted small" }, `失败 ${e.occurrences} 次 · ${e.skill_version || "?"} · 分数 ${fmt.num(e.score, 2)}` + (e.fixed_in ? ` · 修复于 ${e.fixed_in}` : ""))),
  h("dl", { class: "kv" }, h("dt", {}, "根因"), h("dd", {}, e.root_cause || "（尚未分析）"), h("dt", {}, "经验"), h("dd", {}, e.lesson || "—"),
    h("dt", {}, "判分理由"), h("dd", {}, e.judge_reason || "—"), h("dt", {}, "复现"), h("dd", { class: "mono" }, e.repro || "—")),
  h("details", {}, h("summary", {}, "输入 / 期望 / 实际输出 / 轨迹"), h("h3", {}, "输入"), pre(e.input), h("h3", {}, "期望"), pre(e.expected),
    h("h3", {}, "实际输出"), pre(e.output), h("h3", {}, "轨迹摘录"), pre(e.trajectory_excerpt)),
  h("details", {}, h("summary", {}, "标注"), h("form", { onsubmit: async (ev) => {
    ev.preventDefault();
    if (await act(() => api("POST", P(project) + "/errors/" + encodeURIComponent(e.case_id) + "/annotate",
      { category: cat.value, status: st.value, root_cause: rc.value, lesson: lesson.value }), "已保存标注")) route();
  } }, h("div", { class: "row" }, h("label", {}, "根因分类", cat), h("label", {}, "状态", st)), field("根因", rc), field("经验", lesson), h("button", { type: "submit" }, "保存"))));
}

// ----------------------------------------------------------------------------- Review
async function viewReviews(view, project) {
  const filter = h("select", { "aria-label": "状态" }, [["pending", "待标注"], ["done", "已标注"], ["", "全部"]].map(([v, t]) => h("option", { value: v }, t)));
  const list = h("div", {});
  const load = async () => {
    list.replaceChildren(h("p", { class: "muted" }, "加载中……"));
    const r = await act(() => api("GET", P(project) + "/reviews" + qs({ status: filter.value })));
    if (!r) { list.replaceChildren(); return; }
    list.replaceChildren(...(r.reviews.length ? r.reviews.map((x) => reviewCard(project, x, load))
      : [empty(filter.value === "pending" ? "没有待标注的结果。判分器置信度低或使用 human 判分器时，结果会进入此队列。" : "没有记录。")]));
  };
  filter.addEventListener("change", load);
  add(view, [h("div", { class: "head" }, h("h1", {}, "Review 标注"), filter, h("span", { class: "spacer" }),
    h("a", { href: withToken("/api" + P(project) + "/review-sheet") }, "导出最近评测的标注表 (CSV)")), list]);
  await load();
}
function reviewCard(project, x, reload) {
  const comment = h("input", { type: "text", placeholder: "备注（可选）", value: x.comment || "", "aria-label": "备注" });
  const submit = async (passed) => {
    if (await act(() => api("POST", P(project) + "/reviews", { eval_id: x.eval_id, case_id: x.case_id, trial: x.trial, passed, comment: comment.value }), "已提交")) reload();
  };
  return h("div", { class: "panel" }, h("div", { class: "row" }, h("b", { class: "mono" }, x.case_id + (x.trial ? ` #${x.trial}` : "")), badge(x.status),
    x.skill_version ? h("span", { class: "badge info" }, x.skill_version) : null, link(pl(project, "evals/" + x.eval_id), "评测 " + x.eval_id),
    h("span", { class: "spacer" }), h("span", { class: "muted small" }, "自动判分：", x.auto_passed === null ? "—" : (x.auto_passed ? "通过" : "未通过"), ` · ${fmt.num(x.auto_score, 2)}`)),
  x.auto_reason ? h("p", { class: "muted small" }, "自动理由：" + x.auto_reason) : null,
  h("div", { class: "grid" }, h("div", {}, h("h3", {}, "输入"), pre(x.input)), h("div", {}, h("h3", {}, "期望"), pre(x.expected), x.rubric ? [h("h3", {}, "评分标准"), pre(x.rubric)] : null),
    h("div", {}, h("h3", {}, "实际输出"), pre(x.output))),
  x.status === "done" ? h("p", { class: "small" }, "人工结论：", passBadge(!!x.human_passed), x.comment ? " " + x.comment : "", x.reviewer ? `（${x.reviewer}）` : "") : null,
  h("div", { class: "row", style: "margin-top:10px" }, comment, h("button", { class: "primary", onclick: () => submit(true) }, "通过"), h("button", { class: "danger", onclick: () => submit(false) }, "不通过")));
}

// ----------------------------------------------------------------------------- 产物
const ARTIFACT_ZH = { skill: "Skill 版本", eval: "评测报告", best_skill: "最佳 Skill", review_sheet: "Review 标注表", errorbook: "错题库",
  compare_report: "版本比较报告", metrics: "指标摘要", report: "HTML 报告", devset: "开发集" };
async function viewArtifacts(view, project) {
  const [{ artifacts }, { log }] = await Promise.all([api("GET", P(project) + "/artifacts"), api("GET", P(project) + "/gitlog")]);
  const viewer = h("div", { class: "panel" }, empty("从左侧选择一个产物查看内容。"));
  const open = async (a, el) => {
    for (const s of view.querySelectorAll(".list a.sel")) s.classList.remove("sel");
    el.classList.add("sel");
    if (/report\.html$/.test(a.path)) {
      viewer.replaceChildren(h("h2", {}, a.title || a.path), h("iframe", { class: "report", sandbox: "", title: "进化报告", src: withToken("/api" + P(project) + "/report") }));
      return;
    }
    // a skill bundle is registered as a directory: show its SKILL.md
    const isDir = !/\.[A-Za-z0-9]+$/.test(a.path.split("/").pop());
    const r = await act(() => api("GET", P(project) + "/file" + qs({ path: isDir ? a.path.replace(/\/$/, "") + "/SKILL.md" : a.path })));
    if (!r) return;
    let text = r.content;
    if (/\.json$/.test(a.path)) { try { text = JSON.stringify(JSON.parse(text), null, 2); } catch (e) { /* show raw */ } }
    viewer.replaceChildren(h("h2", {}, a.title || a.path), h("p", { class: "mono muted small" }, a.path), pre(text.length > 300000 ? text.slice(0, 300000) + "\n…（已截断）" : text, "tall"));
  };
  add(view, [h("div", { class: "head" }, h("h1", {}, "产物"), h("span", { class: "spacer" }), h("button", { onclick: () => startJob(project, "outputs", {}) }, "重新生成输出"))]);
  if (!artifacts.length) { view.appendChild(empty("还没有产物。运行评测或进化后，SKILL.md 版本、评测报告与输出文件会登记在这里。")); return; }
  const groups = {};
  for (const a of artifacts) (groups[a.kind] = groups[a.kind] || []).push(a);
  const order = ["report", "best_skill", "compare_report", "metrics", "errorbook", "review_sheet", "devset", "skill", "eval"];
  const kinds = Object.keys(groups).sort((x, y) => (order.indexOf(x) + 99) % 99 - (order.indexOf(y) + 99) % 99);
  const list = h("div", { class: "panel list" }, kinds.map((k) => [h("h3", {}, `${ARTIFACT_ZH[k] || k}（${groups[k].length}）`),
    groups[k].slice(0, 60).map((a) => { const el = h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); open(a, el); } }, a.title || a.path.split("/").pop(),
      a.version ? h("span", { class: "muted small" }, " " + a.version) : null); return el; })]));
  view.appendChild(h("div", { class: "split" }, list, viewer));
  if (log.length) view.appendChild(h("div", { class: "panel tablewrap" }, h("h2", {}, "本地 Git 历史（版本库）"), h("table", {},
    h("tbody", {}, log.map((c) => h("tr", {}, h("td", { class: "mono nowrap" }, c.commit), h("td", { class: "nowrap muted" }, c.date), h("td", {}, c.message)))))));
}

// ----------------------------------------------------------------------------- 配置
async function viewConfig(view, project) {
  const [cfg, req] = await Promise.all([api("GET", P(project) + "/config"), api("GET", P(project) + "/requirement")]);
  add(view, [h("div", { class: "head" }, h("h1", {}, "配置"))]);
  const inputs = [];   // {path: [section, key], read: () => value}
  const text = (sec, key, label) => { const el = h("input", { type: "text", value: cfg[sec][key] ?? "" }); inputs.push({ path: [sec, key], read: () => el.value }); return field(label, el); };
  const num = (sec, key, label, step) => { const el = h("input", { type: "number", step: step || "any", value: cfg[sec][key] });
    inputs.push({ path: [sec, key], read: () => { const v = Number(el.value); if (el.value === "" || !isFinite(v)) throw new Error(label + " 必须是数字"); return v; } }); return field(label, el); };
  const bool = (sec, key, label) => { const el = h("input", { type: "checkbox", checked: !!cfg[sec][key] }); inputs.push({ path: [sec, key], read: () => el.checked }); return h("label", { class: "field" }, h("span", {}, label), el); };
  const json = (sec, key, label) => { const el = h("textarea", { rows: 4, value: JSON.stringify(cfg[sec][key] ?? {}, null, 2) });
    inputs.push({ path: [sec, key], read: () => { try { return JSON.parse(el.value || "{}"); } catch (e) { throw new Error(label + " 不是合法 JSON"); } } }); return field(label, el); };
  const sel = (sec, key, label, options) => { const cur = cfg[sec][key]; const el = h("select", {}, (options.includes(cur) ? options : [cur, ...options]).map((o) => h("option", { value: o, selected: o === cur }, o)));
    inputs.push({ path: [sec, key], read: () => el.value }); return field(label, el); };
  const tags = (sec, key, label) => { const el = h("input", { type: "text", value: (cfg[sec][key] || []).join(", ") });
    inputs.push({ path: [sec, key], read: () => el.value.split(",").map((s) => s.trim()).filter(Boolean) }); return field(label, el); };

  view.appendChild(h("form", { onsubmit: async (ev) => {
    ev.preventDefault();
    const patch = {};
    try { for (const { path: [s, k], read } of inputs) (patch[s] = patch[s] || {})[k] = read(); } catch (e) { toast(e.message, "bad"); return; }
    // NOTE: the backend deep-merges, so keys deleted from an options JSON object are not removed.
    if (await act(() => api("PATCH", P(project) + "/config", patch), "配置已保存")) route();
  } },
  h("div", { class: "grid" },
    h("div", { class: "panel" }, h("h2", {}, "被测 Agent"), text("agent", "adapter", "适配器（mock / command / claude_code / codex / nanobot / http / llm / callable 或 pkg.module:Class）"),
      json("agent", "options", "适配器选项（JSON）"), text("agent", "mcp_profile", "Mock MCP Profile（可选）")),
    h("div", { class: "panel" }, h("h2", {}, "框架 LLM（判分 / 分析 / 合并）"), text("llm", "provider", "Provider（mock / anthropic / openai / cli 或 pkg.module:Class）"),
      text("llm", "model", "模型"), json("llm", "options", "选项（JSON）"), json("llm", "roles", "按角色覆盖（JSON）")),
    h("div", { class: "panel" }, h("h2", {}, "评测"), text("eval", "judge", "判分器（exact / contains / regex / numeric / json_match / script / llm / composite / human）"),
      json("eval", "judge_options", "判分器选项（JSON）"), num("eval", "trials", "每例重复次数", 1), num("eval", "concurrency", "并发数", 1),
      num("eval", "timeout_s", "单例超时（秒）"), num("eval", "pass_threshold", "通过阈值（0~1）", 0.05)),
    h("div", { class: "panel" }, h("h2", {}, "进化与闸门"), num("evolve", "max_iterations", "最大迭代轮数", 1), num("evolve", "candidates_per_iteration", "每轮候选数", 1),
      num("evolve", "patience", "无提升容忍轮数", 1), num("evolve", "target_score", "目标分数", 0.05), sel("evolve", "metric", "比较指标", ["mean_score", "pass_rate"]),
      num("evolve", "min_delta", "提升最小增量", 0.005), num("evolve", "max_regressions", "允许回归用例数", 1), tags("evolve", "protect_tags", "受保护标签（逗号分隔，不允许回归）"),
      bool("evolve", "require_human_approval", "提升前需人工批准"), bool("evolve", "confirm_on_test", "在留出 test 上复核"),
      num("evolve", "train_ratio", "train 占比", 0.05), num("evolve", "val_ratio", "val 占比", 0.05), num("evolve", "max_skill_chars", "Skill 最大字符数", 100))),
  h("div", { class: "panel row" }, h("button", { class: "primary", type: "submit" }, "保存配置"), h("span", { class: "muted small" }, "保存到项目目录下的 skillloop.toml；新提交的任务生效。"))));

  const reqEl = h("textarea", { rows: 16, value: req.text || "" });
  view.appendChild(h("form", { class: "panel", onsubmit: async (ev) => {
    ev.preventDefault();
    await act(() => api("PUT", P(project) + "/requirement", { text: reqEl.value }), "需求文档已保存");
  } }, h("h2", {}, "需求文档"), field("业务目标、范围、验收标准、约束", reqEl), h("button", { type: "submit" }, "保存需求文档")));
}

const VIEWS = { "": viewOverview, jobs: viewJobs, versions: viewVersions, evals: viewEvals, errors: viewErrors,
  reviews: viewReviews, artifacts: viewArtifacts, config: viewConfig };

route();
