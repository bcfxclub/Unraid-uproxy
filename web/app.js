/* uproxy WebUI —— 所有接口同源，所有渲染走 textContent，避免注入。 */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let STATE = null;
let toastTimer = null;

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("on"), 3000);
}

/* ---------------- 会话令牌（登录后获得，对用户透明） ----------------
   后端是账号 + 密码登录，登录成功后下发一个随机会话令牌，前端只持有它，
   不再直接接触密码。令牌存 localStorage，并通过 X-UPX-Token 头发送。
   兼容老的 ?token= 直连方式（升级前用旧口令开的链接仍可用）。            */
const SESSION_KEY = "upx_session";
let SESSION = "";

(function initSession() {
  const q = new URLSearchParams(location.search).get("token"); // 兼容老 ?token=
  if (q) {
    SESSION = q.trim();
    try { localStorage.setItem(SESSION_KEY, SESSION); } catch (e) { /* 隐私模式忽略 */ }
  } else {
    try { SESSION = localStorage.getItem(SESSION_KEY) || ""; } catch (e) { SESSION = ""; }
  }
})();

function setSession(v) {
  SESSION = (v || "").trim();
  try { localStorage.setItem(SESSION_KEY, SESSION); } catch (e) { /* ignore */ }
}

function _opt(method, body) {
  const headers = {};
  if (SESSION) headers["X-UPX-Token"] = SESSION;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const opt = { method, headers };
  if (body !== undefined) opt.body = JSON.stringify(body);
  return opt;
}

/* ---------------- 登录 / 登出 ---------------- */
function showLogin(msg) {
  const box = $("#login");
  if (!box) return;
  box.classList.add("on");
  if (msg) $("#loginErr").textContent = msg;
  const u = $("#inUser");
  if (u && !u.value) u.focus();
}
function hideLogin() { const b = $("#login"); if (b) b.classList.remove("on"); }

async function doLogin() {
  const u = $("#inUser").value.trim();
  const p = $("#inPass").value;
  try {
    const r = await fetch("/api/login", _opt("POST", { username: u, password: p }));
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      $("#loginErr").textContent = j.detail || "登录失败";
      return;
    }
    const j = await r.json();
    setSession(j.token);
    $("#who").textContent = j.user || u;
    $("#loginErr").textContent = "";
    hideLogin();
    await loadState();
    loadBackups().catch(() => {});
    Traffic.start();
  } catch (e) {
    $("#loginErr").textContent = e.message || "登录失败";
  }
}

async function doLogout() {
  try { await api("/api/logout", "POST", {}); } catch (e) { /* 忽略 */ }
  setSession("");
  showLogin("已退出登录");
}

$("#btnLogin").addEventListener("click", () => safe(doLogin, "登录"));
$("#inPass").addEventListener("keydown", (e) => { if (e.key === "Enter") safe(doLogin, "登录"); });
$("#inUser").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#inPass").focus(); });
$("#btnLogout").addEventListener("click", () => safe(doLogout, "退出"));

async function api(path, method = "GET", body) {
  let r = await fetch(path, _opt(method, body));
  if (r.status === 401) {
    // 会话失效 / 未登录 -> 回到登录页
    setSession("");
    showLogin("会话已失效，请重新登录");
    throw new Error("未登录");
  }
  let data = null;
  try { data = await r.json(); } catch (e) { data = { ok: false, message: "响应不是 JSON" }; }
  if (!r.ok || data.ok === false) {
    throw new Error(data.message || data.detail || `HTTP ${r.status}`);
  }
  return data;
}

async function safe(fn, label) {
  try { await fn(); } catch (e) { toast(`${label || "操作"}失败：${e.message}`); }
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function pre(text) {
  const p = el("pre", "diff", text || "");
  return p;
}

function pill(text, kind) {
  return el("span", `pill ${kind}`, text);
}

function clear(node) { node.textContent = ""; }

/* ---------------- tabs（侧边栏） ---------------- */
const TITLES = { overview: "概览", outlet: "出口", subs: "订阅", push: "下发", backup: "备份与还原", docs: "文档" };
function switchTab(name) {
  const btn = $$(".nav button").find((x) => x.dataset.tab === name);
  if (!btn) return;
  $$(".nav button").forEach((x) => x.classList.remove("on"));
  $$("section").forEach((x) => x.classList.remove("on"));
  btn.classList.add("on");
  $(`#tab-${name}`).classList.add("on");
  $("#pageTitle").textContent = TITLES[name] || name;
  history.replaceState(null, "", `?${new URLSearchParams({ tab: name }).toString()}`);
}

$$(".nav button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

// 深链：?tab=push 直达指定页签
(function restoreTab() {
  const q = new URLSearchParams(location.search).get("tab");
  if (q && TITLES[q]) switchTab(q);
})();

/* ---------------- 渲染 ---------------- */
function setRunBadge(running, text) {
  const b = $("#runBadge");
  if (!b) return;
  b.classList.toggle("ok", !!running);
  $("#runText").textContent = text || (running ? "正在运行" : "未运行");
}

function renderProbe(p) {
  const t = $("#probeTable");
  clear(t);
  const rows = [
    ["Unraid 版本", p.unraid || "未识别"],
    ["Docker 版本", p.docker_version || "未识别"],
    ["dockerd 当前代理", p.dockerd_proxy || "（无）"],
    ["生效的镜像源", (p.mirrors || []).join("\n") || "（无）"],
    ["CA 商店代理", p.ca_proxy || "（无）"],
    ["系统环境变量", p.system_env || "（无）"],
    ["开机重放块", p.persist_block ? "已写入" : "未写入"],
    ["当前出口地址", p.outlet_url || "（无）"],
  ];
  rows.forEach(([k, v]) => {
    const tr = el("tr");
    tr.appendChild(el("td", "", k));
    tr.appendChild(el("td", "mono", v));
    t.appendChild(tr);
  });
  const head = el("tr");
  head.appendChild(el("td", "", "路径可达性"));
  const td = el("td", "mono");
  Object.entries(p.paths || {}).forEach(([k, v]) => {
    td.appendChild(pill(`${k}: ${v.exists ? "找到" : "缺失"}`, v.exists ? "ok" : "bad"));
    td.appendChild(document.createTextNode(" "));
  });
  head.appendChild(td);
  t.appendChild(head);
  $("#env").textContent = p.host_root ? `HOST_ROOT=${p.host_root}` : "";
  $("#probeHint").textContent = p.unraid ? `Unraid ${p.unraid}` : "";
  const anyPath = Object.values(p.paths || {}).some((x) => x.exists);
  setRunBadge(anyPath || p.docker_version, anyPath ? "已接入宿主机" : "未识别宿主机");
}

function renderConfig(c) {
  const mode = c.outlet.mode;
  $$('input[name="mode"]').forEach((r) => { r.checked = r.value === mode; });
  $$(".pane").forEach((p) => p.classList.remove("on"));
  if (mode !== "none") $(`#pane-${mode}`).classList.add("on");

  $("#inHttp").value = c.outlet.http || "";
  $("#inSocks").value = c.outlet.socks || "";
  $("#inWarpPort").value = c.outlet.warp_port || 9091;
  $("#inWarpImage").value = c.outlet.warp_image || "";
  $("#inWarpEndpointIp").value = c.outlet.warp_endpoint_ip || "";
  $("#inSbPort").value = c.outlet.sb_port || 7890;
  $("#inMirrors").value = (c.mirrors || []).join("\n");
  $("#inNoProxy").value = c.no_proxy || "";
  $("#t-dockerd").checked = !!c.targets.dockerd;
  $("#t-mirror").checked = !!c.targets.mirror;
  $("#t-ca").checked = !!c.targets.ca;
  $("#t-system").checked = !!c.targets.system;
  $("#allowRestart").checked = !!c.allow_restart_docker;
  $("#emhttpProxy").checked = !!c.emhttp_proxy;

  // 出口页的下拉（保留）
  const sel = $("#selNode");
  clear(sel);
  (c.outlet.sb_nodes || []).forEach((n) => {
    const o = el("option", "", `${n.name} · ${n.type}`);
    o.value = n.name;
    sel.appendChild(o);
  });
  if (c.outlet.sb_node) sel.value = c.outlet.sb_node;

  // 订阅源列表 + 节点管理 + 分流设置
  renderSubList(c.outlet.subscriptions || []);
  renderNodes();
  applyRouteUI(c.outlet);
}

async function loadState() {
  const d = await api("/api/state");
  STATE = d;
  renderProbe(d.probe);
  renderConfig(d.config);
}

/* ---------------- 设置保存 ---------------- */
async function save(patch) {
  const d = await api("/api/settings", "POST", patch);
  STATE.config = d.config;
}

$$('input[name="mode"]').forEach((r) => r.addEventListener("change", () => {
  $$(".pane").forEach((p) => p.classList.remove("on"));
  if (r.value !== "none") $(`#pane-${r.value}`).classList.add("on");
  safe(() => save({ outlet: { mode: r.value } }), "切换出口模式");
}));

function bindSave(sel, fn) {
  const n = $(sel);
  if (!n) return;
  const ev = n.tagName === "SELECT" ? "change" : "change";
  n.addEventListener(ev, () => safe(() => save(fn(n)), "保存"));
}

bindSave("#inHttp", (n) => ({ outlet: { http: n.value.trim() } }));
bindSave("#inSocks", (n) => ({ outlet: { socks: n.value.trim() } }));
bindSave("#inWarpPort", (n) => ({ outlet: { warp_port: parseInt(n.value, 10) || 9091 } }));
bindSave("#inWarpImage", (n) => ({ outlet: { warp_image: n.value.trim() } }));
bindSave("#inWarpEndpointIp", (n) => ({ outlet: { warp_endpoint_ip: n.value.trim() } }));
bindSave("#inSbPort", (n) => ({ outlet: { sb_port: parseInt(n.value, 10) || 7890 } }));
bindSave("#inNoProxy", (n) => ({ no_proxy: n.value.trim() }));
bindSave("#inMirrors", (n) => ({ mirrors: n.value.split("\n").map((s) => s.trim()).filter(Boolean) }));
bindSave("#allowRestart", (n) => ({ allow_restart_docker: n.checked }));
bindSave("#emhttpProxy", (n) => ({ emhttp_proxy: n.checked }));
["dockerd", "mirror", "ca", "system"].forEach((k) => {
  bindSave(`#t-${k}`, (n) => ({ targets: { [k]: n.checked } }));
});

/* ---------------- 体检 ---------------- */
$("#btnHealth").addEventListener("click", () => safe(async () => {
  const out = $("#healthOut");
  clear(out);
  out.appendChild(el("div", "hint", "正在探测…"));
  const proxy = $("#healthViaProxy").checked ? (STATE.probe.outlet_url || "") : "";
  const d = await api(`/api/health?proxy=${encodeURIComponent(proxy)}`);
  clear(out);
  const table = el("table", "data");
  const tr = el("tr");
  ["目标", "直连", "耗时", "走代理", "耗时"].forEach((h) => tr.appendChild(el("th", "", h)));
  table.appendChild(tr);
  d.direct.forEach((r, i) => {
    const row = el("tr");
    row.appendChild(el("td", "", r.name));
    row.appendChild(el("td")).appendChild(pill(r.ok ? r.code : `失败 ${r.code}`, r.ok ? "ok" : "bad"));
    row.appendChild(el("td", "mono", r.ms === null ? "-" : `${r.ms}ms`));
    const v = d.via_proxy[i];
    row.appendChild(el("td")).appendChild(
      v ? pill(v.ok ? v.code : `失败 ${v.code}`, v.ok ? "ok" : "bad") : pill("-", "mute"));
    row.appendChild(el("td", "mono", v && v.ms !== null ? `${v.ms}ms` : "-"));
    table.appendChild(row);
  });
  out.appendChild(table);
  if (!proxy) out.appendChild(el("div", "hint", "勾选「同时测走代理」可对比加速效果。"));
}, "体检"));

/* ---------------- 出口探测 ---------------- */
$("#btnProbe").addEventListener("click", () => safe(async () => {
  const url = $("#inHttp").value.trim() || $("#inSocks").value.trim();
  if (!url) { toast("请先填写代理地址"); return; }
  const out = $("#probeOut");
  clear(out);
  out.appendChild(el("div", "hint", "正在探测 CONNECT 能力…"));
  const d = await api("/api/outlet/probe", "POST", { url });
  clear(out);
  out.appendChild(pill(d.verdict, d.ok ? "ok" : "bad"));
  out.appendChild(el("div", "", d.message));
  if (d.ms !== undefined && d.ms !== null) {
    out.appendChild(el("div", "mono", `HTTP ${d.code} · ${d.ms}ms`));
  }
}, "探测"));

/* ---------------- WARP ---------------- */
async function warp(action) {
  const d = await api("/api/outlet/warp", "POST", { action });
  const out = $("#warpOut");
  clear(out);
  if (action === "test") {
    out.appendChild(pill(d.warp === "on" ? "WARP 已生效" : `warp=${d.warp}`, d.warp === "on" ? "ok" : "warn"));
    out.appendChild(el("div", "mono", `出口 IP ${d.ip || "-"} · 机房 ${d.colo || "-"}`));
    out.appendChild(pre(d.raw || ""));
  } else if (action === "status") {
    out.appendChild(pill(d.state || "absent", d.running ? "ok" : "mute"));
    out.appendChild(el("div", "mono", d.endpoint || ""));
  } else if (action === "precheck") {
    out.appendChild(pill(d.ok ? "WARP 可用" : "不建议用 WARP", d.ok ? "ok" : "warn"));
    out.appendChild(el("div", "", d.verdict || ""));
    if (d.suggested_ip) {
      const b = el("button", "", `填入接入点 ${d.suggested_ip}`);
      b.addEventListener("click", () => {
        $("#inWarpEndpointIp").value = d.suggested_ip;
        $("#inWarpEndpointIp").dispatchEvent(new Event("change"));
      });
      out.appendChild(b);
    }
    (d.steps || []).forEach((s) => {
      out.appendChild(el("div", "mono", `${s.ok ? "[通过]" : "[问题]"} ${s.name}：${s.detail}`));
      (s.probes || []).forEach((p) => {
        out.appendChild(el("div", "mono hint",
          `    ${p.ip}  丢包 ${p.loss}%${p.avg != null ? `  平均 ${p.avg.toFixed(0)}ms` : ""}`));
      });
    });
  } else {
    out.appendChild(pill(d.ok ? "成功" : "失败", d.ok ? "ok" : "bad"));
    out.appendChild(el("div", "", d.message || ""));
  }
  return d;
}
$("#btnWarpStart").addEventListener("click", () => safe(() => warp("start"), "启动 WARP"));
$("#btnWarpStop").addEventListener("click", () => safe(() => warp("stop"), "停止 WARP"));
$("#btnWarpPrecheck").addEventListener("click", () => safe(() => warp("precheck"), "WARP 预检"));
$("#btnWarpTest").addEventListener("click", () => safe(() => warp("test"), "验证 WARP"));
$("#btnWarpStatus").addEventListener("click", () => safe(() => warp("status"), "查询状态"));

/* ---------------- 订阅源管理 ---------------- */
function renderSubList(subs) {
  const box = $("#subList");
  clear(box);
  if (!subs.length) { box.appendChild(el("div", "hint", "还没有订阅源。在下面粘贴地址添加，可添加多个并存。")); return; }
  const table = el("table", "data");
  const tr = el("tr");
  ["备注", "地址", "节点", "更新时间", "启用", "操作"].forEach((h) => tr.appendChild(el("th", "", h)));
  table.appendChild(tr);
  subs.forEach((s) => {
    const row = el("tr");
    row.appendChild(el("td", "", s.name || s.url));
    row.appendChild(el("td", "mono", s.url));
    row.appendChild(el("td", "", String(s.count || 0)));
    row.appendChild(el("td", "mono", s.updated_at || "-"));
    const enTd = el("td");
    const en = el("input"); en.type = "checkbox"; en.checked = !!s.enabled;
    en.addEventListener("change", () => safe(async () => {
      const d = await api("/api/subs/update", "POST", { id: s.id, enabled: en.checked });
      toast(d.message); loadState();
    }, "切换启用"));
    enTd.appendChild(en); row.appendChild(enTd);
    const opTd = el("td");
    const up = el("button", "", "更新"); up.className = "mini";
    up.addEventListener("click", () => safe(async () => {
      const d = await api("/api/subs/update", "POST", { id: s.id });
      toast(d.message); loadState();
    }, "更新订阅"));
    const del = el("button", "", "删除"); del.className = "mini bad";
    del.addEventListener("click", () => safe(async () => {
      await api("/api/subs/del", "POST", { id: s.id });
      toast("已删除订阅"); loadState();
    }, "删除订阅"));
    opTd.appendChild(up); opTd.appendChild(del); row.appendChild(opTd);
    table.appendChild(row);
  });
  box.appendChild(table);
}

/* ---------------- 节点管理（v2rayA 风格：网格卡片 + 来源分组 + 搜索 + 延迟） ---------------- */
const nodeState = {
  source: "",        // 当前来源筛选（""=全部）
  q: "",             // 搜索关键词
  view: "grid",     // grid | list
  lastSeq: 0,       // 日志增量拉取游标
};
let nodeCache = [];   // 当前节点全集（含 source）
let delayMap = {};    // name -> {ms, reachable} 缓存测速结果

function nodeSources() {
  const set = new Set(nodeCache.map((n) => n.source).filter(Boolean));
  return Array.from(set).sort();
}

function refreshSourceSel() {
  const sel = $("#nodeSource");
  const cur = sel.value || nodeState.source;
  const sources = nodeSources();
  sel.innerHTML = "";
  const all = el("option", "", "全部");
  all.value = "";
  sel.appendChild(all);
  sources.forEach((s) => sel.appendChild(el("option", "", s)));
  sel.value = cur && sources.includes(cur) ? cur : "";
  nodeState.source = sel.value;
}

function filterNodes() {
  const q = nodeState.q.trim().toLowerCase();
  return nodeCache.filter((n) => {
    if (nodeState.source && n.source !== nodeState.source) return false;
    if (!q) return true;
    const hay = `${n.name} ${n.type} ${n.server} ${n.port}`.toLowerCase();
    return hay.includes(q);
  });
}

function syncSelCount() {
  const dis = new Set(...STATE?.config?.outlet?.sb_disabled || []);
  const selCount = nodeCache.filter((n) => !dis.has(n.name)).length;
  $("#nodeSelCount").textContent = nodeCache.length ? `已选 ${selCount}/${nodeCache.length} 项` : "";
}

function renderNodeGrid() {
  const box = $("#nodeGrid");
  clear(box);
  const items = filterNodes();
  if (!items.length) {
    box.appendChild(el("div", "hint", nodeCache.length ? "无匹配节点" : "暂无节点，请先在上方添加订阅并拉取。"));
    return;
  }
  const dis = new Set(...(STATE?.config?.outlet?.sb_disabled || []));
  const defaultNode = STATE?.config?.outlet?.sb_node || "";
  items.forEach((n) => {
    const card = el("div", "node-card");
    const disabled = dis.has(n.name);
    if (disabled) card.classList.add("off");
    if (defaultNode === n.name) card.classList.add("active");
    const chk = el("input"); chk.type = "checkbox"; chk.checked = !disabled;
    const head = el("div", "nc-head");
    head.appendChild(chk);
    head.appendChild(el("div", "nc-name", n.name));
    const meta = el("div", "nc-meta");
    meta.appendChild(el("span", "nc-type", n.type));
    meta.appendChild(el("span", "nc-src", n.source || "本地"));
    meta.appendChild(el("span", "nc-addr", `${n.server}:${n.port}`));
    card.appendChild(head);
    card.appendChild(meta);
    const d = delayMap[n.name];
    let delayEl;
    if (!d) {
      delayEl = el("span", "nc-delay", "—");
    } else if (d.reachable) {
      delayEl = el("span", `nc-delay ${d.ms < 300 ? "ok" : "warn"}`, `${d.ms}ms`);
    } else {
      delayEl = el("span", "nc-delay bad", "TIMEOUT");
    }
    card.appendChild(delayEl);
    chk.addEventListener("change", () => safe(async () => {
      const all = [...$("#nodeGrid input[type=checkbox]"), ...$("#nodeTbody input[type=checkbox]")];
      // 重新计算全集禁用：基于 cache 全集
      const disSet = new Set();
      nodeCache.forEach((nn) => {
        const c = document.querySelector(`.node-card[data-n="${CSS.escape(nn.name)}"] input[type=checkbox]`);
        const row = document.querySelector(`#nodeTbody tr[data-n="${CSS.escape(nn.name)}"] input[type=checkbox]`);
        const checked = c ? c.checked : (row ? row.checked : !disSet.has(nn.name));
        if (!checked) disSet.add(nn.name);
      });
      const d = await api("/api/nodes", "POST", { disabled: Array.from(disSet) });
      loadState();
    }, "更新节点"));
    card.setAttribute("data-n", n.name);
    card.addEventListener("click", (e) => {
      if (e.target.tagName === "INPUT") return;
      chk.click();
    });
    box.appendChild(card);
  });
}

function renderNodeTable(nodes, sbNode) {
  const tb = $("#nodeTbody");
  clear(tb);
  const items = filterNodes();
  if (!items.length) {
    tb.appendChild(el("tr")).appendChild(el("td", "", nodeCache.length ? "无匹配节点" : "暂无节点，请先在上方添加订阅并拉取。")).setAttribute("colspan", "6");
    return;
  }
  const dis = new Set(...(STATE?.config?.outlet?.sb_disabled || []));
  items.forEach((n) => {
    const row = el("tr");
    row.setAttribute("data-n", n.name);
    // 启用
    const enTd = el("td");
    const en = el("input"); en.type = "checkbox"; en.checked = !dis.has(n.name);
    en.addEventListener("change", () => safe(async () => {
      const disSet = new Set();
      $$("#nodeGrid .node-card").forEach((c) => {
        if (!c.querySelector("input")?.checked) disSet.add(c.getAttribute("data-n"));
      });
      $$("#nodeTbody tr").forEach((r) => {
        if (!r.querySelector("input")?.checked) disSet.add(r.getAttribute("data-n"));
      });
      const d = await api("/api/nodes", "POST", { disabled: Array.from(disSet) });
      loadState();
    }, "更新节点"));
    enTd.appendChild(en); row.appendChild(enTd);
    // 名称（可改名）
    const nameTd = el("td");
    const inp = el("input"); inp.value = n.name; inp.className = "name-edit";
    inp.addEventListener("change", () => safe(async () => {
      const to = inp.value.trim();
      if (!to || to === n.name) return;
      await api("/api/nodes", "POST", { rename: { from: n.name, to } });
      toast("已重命名"); loadState();
    }, "重命名"));
    nameTd.appendChild(inp); row.appendChild(nameTd);
    // 来源
    row.appendChild(el("td", "", n.source || "本地"));
    // 类型
    row.appendChild(el("td")).appendChild(pill(n.type, "mute"));
    // 测速
    const tTd = el("td", "", "—"); tTd.setAttribute("data-name", n.name); row.appendChild(tTd);
    // 默认
    const defTd = el("td");
    const r = el("input"); r.type = "radio"; r.name = "defNode"; r.checked = (n.name === sbNode);
    r.addEventListener("change", () => safe(async () => {
      await api("/api/nodes", "POST", { node: n.name });
      loadState();
    }, "设默认"));
    defTd.appendChild(r); row.appendChild(defTd);
    tb.appendChild(row);
  });
}

function renderNodes() {
  const cfg = STATE?.config?.outlet;
  nodeCache = cfg?.sb_nodes || [];
  const dis = new Set(cfg?.sb_disabled || []);
  refreshSourceSel();
  syncSelCount();
  // 视图切换：grid 显示网格隐藏表，反之
  const gridOn = nodeState.view === "grid";
  $("#nodeGrid").style.display = gridOn ? "block" : "none";
  $("#nodeTableWrap").style.display = gridOn ? "none" : "block";
  $("#btnNodeGrid").classList.toggle("primary", gridOn);
  $("#btnNodeList").classList.toggle("primary", !gridOn);
  if (gridOn) {
    renderNodeGrid();
  }
  // 列表 tbody 始终渲染（隐藏也不影响测速写回）
  renderNodeTable(nodeCache, cfg?.sb_node || "");
}

$("#nodeSearch").addEventListener("input", (e) => { nodeState.q = e.target.value; if (nodeState.view === "grid") renderNodeGrid(); else renderNodeTable(nodeCache, STATE?.config?.outlet?.sb_node || ""); });
$("#nodeSource").addEventListener("change", (e) => { nodeState.source = e.target.value; renderNodes(); });
$("#btnNodeGrid").addEventListener("click", () => { nodeState.view = "grid"; renderNodes(); });
$("#btnNodeList").addEventListener("click", () => { nodeState.view = "list"; renderNodes(); });

$("#btnNodeTest").addEventListener("click", () => safe(async () => {
  const out = $("#subOut"); out.appendChild(el("div", "hint", "正在测速（TCP 连通耗时）…"));
  const d = await api("/api/subs/test", "POST", {});
  delayMap = {};
  (d.results || []).forEach((r) => { delayMap[r.tag] = r; });
  // 把测速结果写回网格与列表
  $$("#nodeGrid .node-card").forEach((c) => {
    const name = c.getAttribute("data-n");
    const r = delayMap[name];
    let de = c.querySelector(".nc-delay");
    if (!de) { de = el("span", "nc-delay"); c.appendChild(de); }
    if (!r) { de.className = "nc-delay"; de.textContent = "—"; return; }
    de.className = r.reachable ? `nc-delay ${r.ms < 300 ? "ok" : "warn"}` : "nc-delay bad";
    de.textContent = r.reachable ? `${r.ms}ms` : "TIMEOUT";
  });
  $$("#nodeTbody tr").forEach((row) => {
    const td = row.querySelector("td[data-name]");
    if (!td) return;
    const r = delayMap[td.getAttribute("data-name")];
    if (!r) return;
    td.textContent = "";
    td.appendChild(pill(r.reachable ? `${r.ms}ms` : "不可达", r.reachable ? (r.ms < 300 ? "ok" : "warn") : "bad"));
  });
  const bad = (d.results || []).filter((r) => !r.reachable).length;
  out.appendChild(el("div", "hint", `测速完成：不可达 ${bad} 个（TCP 耗时，真实延迟略高）。`));
}, "节点测速"));

function applyRouteUI(outlet) {
  const mode = outlet.sb_route || "auto";
  $$(`input[name=routeMode]`).forEach((r) => { r.checked = (r.value === mode); });
  $("#customRoute").style.display = (mode === "custom") ? "block" : "none";
  $("#inDirect").value = (outlet.sb_direct || []).join("\n");
  $("#inProxyDomains").value = (outlet.sb_proxy || []).join("\n");
  $("#sbTun").checked = !!outlet.sb_tun;
  $("#inAutoUpdate").value = String(outlet.sb_auto_update || 0);
  const hint = $("#sbRouteHint");
  if (hint) hint.textContent = `当前分流模式：${mode === "auto" ? "智能分流（国内/局域网直连，仅目标域名走代理）" : mode === "all" ? "全部走代理（谨慎）" : "自定义"}。`;
}

$("#btnSubAdd").addEventListener("click", () => safe(async () => {
  const url = $("#inSubUrl").value.trim();
  if (!url && !$("#inSubRaw").value.trim()) { toast("请填写订阅地址或粘贴内容"); return; }
  const out = $("#subOut");
  clear(out); out.appendChild(el("div", "hint", "正在添加并拉取订阅…"));
  const d = await api("/api/subs/add", "POST", {
    name: $("#inSubName").value.trim(),
    url,
    ua: $("#inSubUa").value.trim() || "clash.meta",
    proxy: $("#inSubProxy").value.trim(),
    enabled: $("#inSubEnabled").checked,
  });
  clear(out);
  out.appendChild(pill(d.ok ? "完成" : "失败", d.ok ? "ok" : "bad"));
  out.appendChild(el("div", "", d.message));
  if (d.summary) d.summary.forEach((s) => out.appendChild(el("div", "mono", `${s.name}: ${s.ok ? s.message : s.message}`)));
  loadState();
}, "添加订阅"));

$("#btnSubUpdateAll").addEventListener("click", () => safe(async () => {
  const out = $("#subOut"); clear(out); out.appendChild(el("div", "hint", "正在更新全部订阅…"));
  const d = await api("/api/subs/update", "POST", {});
  clear(out);
  out.appendChild(pill(d.ok ? "完成" : "失败", d.ok ? "ok" : "bad"));
  out.appendChild(el("div", "", d.message));
  loadState();
}, "更新全部订阅"));

$("#btnSubRaw").addEventListener("click", () => safe(async () => {
  const raw = $("#inSubRaw").value.trim();
  if (!raw) { toast("请先粘贴订阅内容"); return; }
  const out = $("#subOut"); clear(out); out.appendChild(el("div", "hint", "正在解析粘贴内容…"));
  const d = await api("/api/subs/add", "POST", {
    name: $("#inSubName").value.trim() || "本地粘贴",
    raw,
    enabled: true,
  });
  clear(out);
  out.appendChild(pill(d.ok ? "完成" : "失败", d.ok ? "ok" : "bad"));
  out.appendChild(el("div", "", d.message));
  if (d.unsupported && d.unsupported.length) {
    out.appendChild(el("div", "hint", `不支持的协议：${d.unsupported.join(", ")}（已跳过）`));
  }
  loadState();
}, "解析粘贴内容"));

$$(`input[name=routeMode]`).forEach((r) => r.addEventListener("change", () => {
  $("#customRoute").style.display = (r.value === "custom") ? "block" : "none";
}));

$("#btnRouteSave").addEventListener("click", () => safe(async () => {
  const mode = ($$(`input[name=routeMode]`).find((r) => r.checked) || {}).value || "auto";
  await api("/api/settings", "POST", {
    outlet: {
      sb_route: mode,
      sb_direct: $("#inDirect").value.split("\n").map((s) => s.trim()).filter(Boolean),
      sb_proxy: $("#inProxyDomains").value.split("\n").map((s) => s.trim()).filter(Boolean),
      sb_tun: $("#sbTun").checked,
      sb_auto_update: parseInt($("#inAutoUpdate").value || "0", 10) || 0,
    },
  });
  toast("分流设置已保存"); loadState();
}, "保存分流设置"));

/* ---------------- sing-box ---------------- */
async function sb(action) {
  const d = await api("/api/outlet/singbox", "POST", {
    action,
    node: $("#selNode").value || "",
  });
  const out = $("#sbOut");
  clear(out);
  if (action === "status") {
    out.appendChild(pill(d.state || "absent", d.running ? "ok" : "mute"));
  } else {
    out.appendChild(pill(d.ok ? "成功" : "失败", d.ok ? "ok" : "bad"));
  }
  out.appendChild(el("div", "", d.message || d.endpoint || ""));
}
$("#btnSbStart").addEventListener("click", () => safe(() => sb("start"), "启动 sing-box"));
$("#btnSbStop").addEventListener("click", () => safe(() => sb("stop"), "停止 sing-box"));

/* ---------------- 镜像源 ---------------- */
$("#btnMirrorCheck").addEventListener("click", () => safe(async () => {
  const out = $("#mirrorOut");
  clear(out);
  out.appendChild(el("div", "hint", "正在逐个探测 /v2/ …"));
  const d = await api("/api/mirrors/check?force=1");
  clear(out);
  const table = el("table", "data");
  const tr = el("tr");
  ["镜像源", "HTTP", "耗时"].forEach((h) => tr.appendChild(el("th", "", h)));
  table.appendChild(tr);
  (d.results || []).forEach((r) => {
    const row = el("tr");
    row.appendChild(el("td", "mono", r.mirror));
    row.appendChild(el("td")).appendChild(pill(r.code, r.alive ? "ok" : "bad"));
    row.appendChild(el("td", "mono", r.ms === null ? "-" : `${r.ms}ms`));
    table.appendChild(row);
  });
  out.appendChild(table);
  out.appendChild(el("div", "hint", d.cached ? "（来自缓存）" : "（已按响应速度排序，401/403 表示服务在线）"));
}, "镜像源体检"));

$("#btnMirrorAuto").addEventListener("click", () => safe(async () => {
  const d = await api("/api/mirrors/auto?limit=3", "POST", {});
  $("#inMirrors").value = (d.mirrors || []).join("\n");
  toast(`已保留最快 3 个：${(d.picked || []).join(", ")}`);
}, "自动排序"));

/* ---------------- 下发 ---------------- */
function renderPlan(plan) {
  const out = $("#planOut");
  clear(out);
  (plan || []).forEach((it) => {
    const box = el("div");
    const head = el("div", "row");
    head.appendChild(el("strong", "", it.title));
    head.appendChild(pill(
      it.status || (it.changed ? "待写入" : "无需改动"),
      it.status === "written" ? "ok" : it.status === "error" ? "bad" : it.changed ? "warn" : "mute"));
    if (!it.ok) head.appendChild(pill("跳过", "bad"));
    box.appendChild(head);
    box.appendChild(el("div", "mono", it.path || ""));
    if (it.note) box.appendChild(el("div", "hint", it.note));
    if (it.changed || it.status === "written") {
      box.appendChild(el("div", "hint", "— 当前 —"));
      box.appendChild(pre(it.current || ""));
      box.appendChild(el("div", "hint", "— 将变为 —"));
      box.appendChild(pre(it.proposed || ""));
    }
    out.appendChild(box);
  });
}

$("#btnPlan").addEventListener("click", () => safe(async () => {
  const d = await api("/api/plan");
  renderPlan(d.plan);
}, "生成预览"));

$("#btnApply").addEventListener("click", () => safe(async () => {
  if (!confirm("确认写入？会先备份原文件。若勾选了立即重启 Docker，所有容器会按自动启动设置重启。")) return;
  const d = await api("/api/apply", "POST", {
    keys: ["dockerd", "mirror", "ca", "system"].filter((k) => $(`#t-${k}`).checked),
    restart: $("#restartNow").checked,
    persist: true,
  });
  const plan = d.results || [];
  if (d.persist) plan.push(d.persist);
  renderPlan(plan);
  toast("已应用");
  await loadState();
}, "应用"));

/* ---------------- 备份 ---------------- */
async function loadBackups() {
  const d = await api("/api/backups");
  const out = $("#bakOut");
  clear(out);
  if (!(d.backups || []).length) { out.appendChild(el("div", "hint", "暂无备份")); return; }
  const table = el("table", "data");
  const tr = el("tr");
  ["文件", "大小", "时间"].forEach((h) => tr.appendChild(el("th", "", h)));
  table.appendChild(tr);
  d.backups.forEach((b) => {
    const row = el("tr");
    row.appendChild(el("td", "mono", b.file));
    row.appendChild(el("td", "mono", `${b.size}B`));
    row.appendChild(el("td", "mono", b.mtime));
    table.appendChild(row);
  });
  out.appendChild(table);
}
$("#btnRefreshBak").addEventListener("click", () => safe(loadBackups, "刷新备份"));
$("#btnRestore").addEventListener("click", () => safe(async () => {
  if (!confirm("确认还原？会用最近一次备份覆盖当前文件。")) return;
  const d = await api("/api/restore", "POST", { key: $("#selBackupKey").value });
  toast(d.message);
  await loadBackups();
  await loadState();
}, "还原"));
$("#btnUninstall").addEventListener("click", () => safe(async () => {
  if (!confirm("确认移除 /boot/config/go 里的托管块？")) return;
  const d = await api("/api/uninstall", "POST", {});
  $("#uninstOut").textContent = d.go.message || d.go.status;
  await loadState();
}, "卸载"));

/* ---------------- 启动：先看是否要登录 ---------------- */
async function boot() {
  let info = { required: false };
  try {
    info = await (await fetch("/api/auth/info")).json();
  } catch (e) { info = { required: false }; }

  if (info.required) {
    if (!SESSION) { showLogin(); return; }
    // 验证已有会话是否仍然有效
    try {
      await api("/api/state");
    } catch (e) {
      showLogin("请登录");
      return;
    }
    $("#who").textContent = info.user || "";
  }
  await loadState().catch((e) => toast(`初始化失败：${e.message}`));
  loadBackups().catch(() => {});
  Traffic.start();
  if (typeof LogPage !== "undefined") LogPage.boot();
}

/* ---------------- 实时流量图（仿 v2rayA） ---------------- */
const Traffic = (function () {
  const MAX = 60;
  let down = [], up = [];
  let canvas = null, ctx = null, timer = null;

  function resize() {
    if (!canvas) return;
    const w = canvas.clientWidth || 600;
    const h = 160;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }

  function fmt(b) {
    b = Number(b) || 0;
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return (b < 10 && i > 0 ? b.toFixed(2) : b.toFixed(1)) + " " + u[i];
  }

  function draw() {
    if (!ctx || !canvas) return;
    const w = canvas.clientWidth || 600, h = 160;
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.lineWidth = 1;
    for (let g = 1; g < 4; g++) {
      const y = h * g / 4;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }
    const all = down.concat(up);
    const max = Math.max(1, ...all);
    const step = w / (MAX - 1);
    [{ data: down, color: "#4fd08a", fill: "rgba(79,208,138,0.18)" },
     { data: up, color: "#e6b64f", fill: "rgba(230,182,79,0.18)" }].forEach((s) => {
      if (s.data.length < 2) return;
      const x0 = w - (s.data.length - 1) * step;
      const yOf = (v) => h - (v / max) * (h - 12) - 6;
      ctx.beginPath();
      s.data.forEach((v, i) => { const x = x0 + i * step; i ? ctx.lineTo(x, yOf(v)) : ctx.moveTo(x, yOf(v)); });
      ctx.lineTo(x0 + (s.data.length - 1) * step, h);
      ctx.lineTo(x0, h);
      ctx.closePath();
      ctx.fillStyle = s.fill; ctx.fill();
      ctx.beginPath();
      s.data.forEach((v, i) => { const x = x0 + i * step; i ? ctx.lineTo(x, yOf(v)) : ctx.moveTo(x, yOf(v)); });
      ctx.strokeStyle = s.color; ctx.lineWidth = 1.5; ctx.stroke();
    });
  }

  function reset() {
    down = []; up = [];
    $("#tDown").textContent = "0 B/s";
    $("#tUp").textContent = "0 B/s";
    $("#tConns").textContent = "—";
    $("#tDownTot").textContent = "0 B";
    $("#tUpTot").textContent = "0 B";
    draw();
  }

  function push(d) {
    down.push(d.down_rate || 0);
    up.push(d.up_rate || 0);
    if (down.length > MAX) { down.shift(); up.shift(); }
    $("#tDown").textContent = fmt(d.down_rate) + "/s";
    $("#tUp").textContent = fmt(d.up_rate) + "/s";
    $("#tConns").textContent = (d.conns == null ? "—" : d.conns);
    $("#tDownTot").textContent = fmt(d.down_bytes || 0);
    $("#tUpTot").textContent = fmt(d.up_bytes || 0);
    // 各节点连接分布
    const box = $("#tNodes");
    const by = d.conns_by_node || {};
    const entries = Object.entries(by).sort((a, b) => b[1] - a[1]);
    box.innerHTML = "";
    if (!entries.length) {
      box.appendChild(el("span", "muted", "暂无活跃连接"));
    } else {
      entries.forEach(([tag, n]) => {
        const chip = el("span", "nchip", `${tag} · ${n}`);
        box.appendChild(chip);
      });
    }
    draw();
  }

  async function poll() {
    try {
      const d = await api("/api/outlet/stats");
      if (!d.running) {
        $("#tHint").textContent = "未启用出口（「出口」页启动 WARP / sing-box 后显示）";
        reset();
        return;
      }
      $("#tHint").textContent = d.mode === "warp" ? "出口：Cloudflare WARP" : "出口：sing-box";
      push(d);
    } catch (e) { /* 401 等由 api 统一处理 */ }
  }

  function start() {
    canvas = $("#trafficChart");
    if (!canvas) return;
    ctx = canvas.getContext("2d");
    resize();
    window.addEventListener("resize", resize);
    if (timer) return;
    timer = setInterval(poll, 1500);
    poll();
  }

  return { start };
})();

/* ---------------- 日志页（仿 v2rayA「日志」） ---------------- */
const LogPage = (function () {
  let timer = null;
  let activeLevel = "";
  let searchQ = "";
  let activeScope = "";
  const rows = [];   // 已渲染日志（最新在末尾）

  function lvBadge(lv) {
    const map = { error: "bad", warn: "warn", debug: "mute", trace: "mute", info: "ok" };
    return el("span", `lv lv-${lv}`, lv.toUpperCase().slice(0, 5));
  }

  function renderLog() {
    const body = $("#logBody");
    const q = searchQ.trim().toLowerCase();
    const shown = rows.filter((r) => {
      if (activeScope && r.scope !== activeScope) return false;
      if (!q) return true;
      return `${r.level} ${r.scope} ${r.msg}`.toLowerCase().includes(q);
    });
    clear(body);
    shown.slice(-500).forEach((r) => {
      const line = el("div", `logline lv-${r.level}`);
      const ts = el("span", "lt", r.ts);
      line.appendChild(lvBadge(r.level));
      line.appendChild(el("span", "ls", `[${r.scope}]`));
      const msg = el("span", "lm", r.msg);
      line.appendChild(msg);
      body.appendChild(line);
    });
    $("#logCount").textContent = shown.length ? `${shown.length} 条` : "";
    body.scrollTop = body.scrollHeight;
  }

  async function refresh(scopes) {
    try {
      const d = await api(`/api/logs?since=${nodeState.lastSeq}&level=${encodeURIComponent(activeLevel)}&limit=200`);
      (d.logs || []).forEach((r) => {
        if (r.seq > nodeState.lastSeq) { rows.push(r); nodeState.lastSeq = r.seq; }
      });
      if (scopes) refreshScopes(d);
      renderLog();
    } catch (e) { /* 401 由 api 统一处理 */ }
  }

  async function refreshScopes() {
    try {
      const d = await api("/api/logs/scopes");
      const sel = $("#logScopeSel");
      const cur = sel.value || activeScope;
      sel.innerHTML = "";
      sel.appendChild(el("option", "", "全部来源"));
      (d.scopes || []).forEach((s) => sel.appendChild(el("option", "", s)));
      sel.value = cur && (d.scopes || []).includes(cur) ? cur : "";
      activeScope = sel.value;
    } catch (e) { /* 忽略 */ }
  }

  function startTimer() {
    if (timer) clearInterval(timer);
    const ms = parseInt($("#logAutoInt").value || "0", 10);
    if (ms > 0) timer = setInterval(() => refresh(false), ms);
  }

  async function boot() {
    // 初次载入：拉全量
    nodeState.lastSeq = 0; rows.length = 0;
    await refresh(false);
    refreshScopes();
    startTimer();
  }

  // 级别标签
  $$("#logLevelTabs .lv").forEach((b) => b.addEventListener("click", () => {
    $$("#logLevelTabs .lv").forEach((x) => x.classList.remove("on"));
    b.classList.add("on");
    activeLevel = b.dataset.lv;
    rows.length = 0; nodeState.lastSeq = 0;
    refresh(false);
  }));
  $("#logSearch").addEventListener("input", (e) => { searchQ = e.target.value; renderLog(); });
  $("#logScopeSel").addEventListener("change", (e) => { activeScope = e.target.value; renderLog(); });
  $("#logAutoInt").addEventListener("change", startTimer);
  $("#btnLogExport").addEventListener("click", () => safe(async () => {
    const d = await api(`/api/logs/export?level=${encodeURIComponent(activeLevel)}`);
    const blob = new Blob([d.text || ""], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = el("a"); a.href = url; a.download = `uproxy-logs-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "")}.txt`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("日志已导出");
  }, "导出日志"));

  return { boot };
})();

boot();
