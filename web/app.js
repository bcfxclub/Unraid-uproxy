/* uproxy WebUI —— 所有接口同源，所有渲染走 textContent，避免注入。 */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let STATE = null;
let toastTimer = null;

function toast(msg, ms) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("on");
  clearTimeout(toastTimer);
  // ms = 0 表示常驻（用来挂「正在更新…」，出结果时被下一条提示替换）
  const hold = ms === undefined ? 3000 : ms;
  if (hold > 0) toastTimer = setTimeout(() => el.classList.remove("on"), hold);
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
    if (typeof Account !== "undefined") Account.boot();
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

/* ---------------- 悬浮菜单（⋮）---------------- */
const Pop = (function () {
  let cur = null;
  let curAnchor = null;
  function close() {
    if (!cur) return;
    cur.remove();
    cur = null;
    curAnchor = null;
    document.removeEventListener("click", onDoc, true);
    document.removeEventListener("keydown", onKey, true);
  }
  function onDoc(e) {
    if (!cur) return;
    if (cur.contains(e.target)) return;
    // 点触发器本身交给 toggle 处理，否则会「先关再开」
    if (curAnchor && (e.target === curAnchor || curAnchor.contains(e.target))) return;
    if (e.target.classList && e.target.classList.contains("dots")) return;
    close();
  }
  function onKey(e) { if (e.key === "Escape") close(); }

  /* items 每项：
       {sep:true}                       分隔线
       {node: 元素}                     自定义内容（行内按钮自己控制是否关闭）
       {label, danger, disabled, onClick} 普通按钮                              */
  function open(anchor, items, opts) {
    close();
    const o = opts || {};
    const m = el("div", "pop" + (o.cls ? ` ${o.cls}` : ""));
    items.forEach((it) => {
      if (it.sep) { m.appendChild(el("div", "pop-sep")); return; }
      if (it.node) { m.appendChild(it.node); return; }
      const b = el("button", it.danger ? "danger" : "", it.label);
      if (it.disabled) b.disabled = true;
      b.addEventListener("click", () => { close(); if (it.onClick) it.onClick(); });
      m.appendChild(b);
    });
    document.body.appendChild(m);
    const r = anchor.getBoundingClientRect();
    let left = r.right - m.offsetWidth;
    if (left < 8) left = 8;
    if (left + m.offsetWidth > innerWidth - 8) left = innerWidth - m.offsetWidth - 8;
    let top = r.bottom + 6;
    if (top + m.offsetHeight > innerHeight - 8) top = Math.max(8, r.top - m.offsetHeight - 6);
    m.style.left = `${Math.max(8, left)}px`;
    m.style.top = `${top}px`;
    cur = m;
    curAnchor = anchor;
    setTimeout(() => {
      document.addEventListener("click", onDoc, true);
      document.addEventListener("keydown", onKey, true);
    }, 0);
  }

  function toggle(anchor, items, opts) {
    if (cur && curAnchor === anchor) { close(); return; }
    open(anchor, items, opts);
  }
  return { open, toggle, close };
})();

/* 触发 ⋮ 的三点按钮 */
function dotsBtn(onClick) {
  const b = el("button", "dots", "⋮");
  b.type = "button";
  b.addEventListener("click", (e) => { e.stopPropagation(); onClick(b); });
  return b;
}

/* 卡片上常显的「删除」按钮（垃圾桶图标），不用展开菜单就能点 */
const TRASH_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
  + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
  + '<polyline points="3 6 5 6 21 6"/>'
  + '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'
  + '<path d="M10 11v6"/><path d="M14 11v6"/></svg>';

function icoDel(title, onClick) {
  const b = el("button", "ico-del");
  b.type = "button";
  b.title = title || "删除";
  b.innerHTML = TRASH_SVG;
  b.addEventListener("click", (e) => { e.stopPropagation(); e.preventDefault(); onClick(); });
  return b;
}

/* ---------------- 通用弹窗（改名称 / 修改订阅 / 分享链接）---------------- */
const Modal = (function () {
  function build(opts) {
    return new Promise((resolve) => {
      const mask = el("div", "modal-mask");
      const box = el("div", "modal");
      box.appendChild(el("h3", "", opts.title || ""));
      if (opts.note) box.appendChild(el("p", "modal-note", opts.note));

      const fields = [];
      let area = null;

      if (opts.mode === "text") {
        area = el("textarea", "modal-area");
        area.rows = opts.rows || 6;
        area.readOnly = true;
        area.value = opts.value || "";
        box.appendChild(area);
      } else if (Array.isArray(opts.fields)) {
        opts.fields.forEach((f) => {
          if (f.label) box.appendChild(el("label", "modal-lbl", f.label));
          const inp = el("input", "modal-input");
          inp.value = f.value || "";
          inp.placeholder = f.placeholder || "";
          if (f.readonly) inp.readOnly = true;
          box.appendChild(inp);
          fields.push({ key: f.key, input: inp });
        });
      } else {
        const inp = el("input", "modal-input");
        inp.value = opts.value || "";
        inp.placeholder = opts.placeholder || "";
        box.appendChild(inp);
        fields.push({ key: "value", input: inp });
      }

      const row = el("div", "modal-btns");
      const cancel = el("button", "", opts.cancelText || "取消");
      const ok = el("button", "primary", opts.okText || "确定");
      const done = (val) => { mask.remove(); resolve(val); };
      cancel.addEventListener("click", () => done(null));
      ok.addEventListener("click", () => {
        if (opts.mode === "text") { done(area.value); return; }
        const out = {};
        fields.forEach((f) => { out[f.key] = f.input.value.trim(); });
        done(out);
      });
      row.append(cancel, ok);
      box.appendChild(row);
      mask.appendChild(box);
      mask.addEventListener("click", (e) => { if (e.target === mask) done(null); });
      mask.addEventListener("keydown", (e) => {
        if (e.key === "Escape") done(null);
        if (e.key === "Enter" && opts.mode !== "text") ok.click();
      });
      document.body.appendChild(mask);
      const first = opts.mode === "text" ? area : (fields[0] && fields[0].input);
      if (first) { first.focus(); if (opts.mode === "text") first.select(); }
    });
  }
  return { ask: build };
})();

async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* 回退 */ }
  try {
    const ta = el("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch (e) { return false; }
}

/* ---------------- tabs（侧边栏） ---------------- */
const TITLES = { overview: "仪表板", outlet: "出口", subs: "订阅", push: "下发", backup: "备份与还原", logs: "日志", account: "账号", docs: "文档" };
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

  // 订阅源列表 + 节点管理 + 分流规则
  renderSubList(c.outlet.subscriptions || []);
  renderNodes();
  Route.boot(c.outlet);
  Dash.render();
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

/* ---------------- 订阅源管理（卡片 + 批量） ---------------- */
const subSel = new Set();

function subKey(s) { return s.id || s.url || s.name || ""; }

function subTitle(s) {
  const n = (s.name || "").trim();
  if (n) return n;
  const u = (s.url || "").replace(/^https?:\/\//, "");
  return u.split("/")[0] || "未命名订阅";
}

function syncSubSel() {
  const subs = STATE?.config?.outlet?.subscriptions || [];
  const n = subSel.size;
  const c = $("#subSelCount");
  if (c) c.textContent = subs.length ? `已选 ${n} 项 / 共 ${subs.length} 个` : "";
  ["#btnSubDelSel", "#btnSubUpdSel"].forEach((sel) => {
    const b = $(sel);
    if (b) b.disabled = !n;
  });
  const all = $("#subSelAll");
  if (all) {
    all.checked = subs.length > 0 && n === subs.length;
    all.indeterminate = n > 0 && n < subs.length;
  }
}

/* 把节点导出成分享链接（订阅和节点两处共用） */
async function shareNodes(names) {
  if (!names || !names.length) { toast("请先选择节点"); return; }
  const d = await api("/api/nodes/share", "POST", { names });
  if (!d.items || !d.items.length) { toast(d.message || "没有可导出的节点"); return; }
  const v = await Modal.ask({
    title: `分享 ${d.items.length} 个节点`,
    note: "下面的链接可直接导入 v2rayN / Clash / sing-box 等客户端。",
    mode: "text",
    rows: 7,
    value: d.text,
    okText: "复制并关闭",
  });
  if (v === null) return;
  const ok = await copyText(d.text);
  toast(ok ? "已复制到剪贴板" : "请手动全选复制");
}

async function delSubs(keys, label) {
  if (!keys.length) { toast("请先选择订阅"); return; }
  const tip = keys.length === 1
    ? `确认删除订阅「${label || keys[0]}」？该订阅下的节点会一并移除。`
    : `确认删除选中的 ${keys.length} 个订阅？这些订阅下的节点会一并移除。`;
  if (!confirm(tip)) return;
  const d = await api("/api/subs/del", "POST", { ids: keys });
  subSel.clear();
  toast(`已删除 ${d.deleted || keys.length} 个订阅`);
  await loadState();
}

/* 订阅更新中（同一时间只跑一轮，避免连点把节点列表刷乱） */
let subBusy = false;

function cardSel(id) {
  return `[data-key="${String(id).replace(/"/g, '\\"')}"]`;
}

/* 更新完的一句话结论：哪条订阅、成没成、拿到多少节点、用量多少 */
function subResultLine(sub, d) {
  const name = subTitle(sub);
  const s = (d.summary || []).find((x) => (x.url || "") === (sub.url || "")) || {};
  if (d.sub_ok === false || s.ok === false) {
    return `${name} 更新失败：${d.sub_message || s.message || "拉取失败"}`;
  }
  const bits = [`${name} 已更新`];
  if (sub.count) bits.push(`${sub.count} 个节点`);
  if (hasUsage(sub)) bits.push(`已用 ${bytes2(sub.usage.used)} / ${bytes2(sub.usage.total)}`);
  return bits.join(" · ");
}

async function updateSubs(ids) {
  if (!ids.length) { toast("请先选择订阅"); return; }
  if (subBusy) { toast("正在更新，等这一轮跑完"); return; }
  const all = STATE?.config?.outlet?.subscriptions || [];
  const targets = ids.map((id) => all.find((s) => subKey(s) === id || s.id === id) || { id });
  // 订阅页卡片和首页订阅行都要点亮：首页那一份虽然藏在别的页签里，
  // 但 querySelector 会先命中它，所以这里两处都取，别只标到看不见的那个
  const cards = ids.flatMap((id) => Array.from(
    document.querySelectorAll(`.subcard${cardSel(id)}, .dsub${cardSel(id)}`)));

  subBusy = true;
  try {
    cards.forEach((c) => {
      c.classList.add("busy");
      const b = c.querySelector(".dots");
      if (b && b.parentNode) {
        // 卡片上挂一枚「更新中」，比只弹一下提示更耐看
        if (!c.querySelector(".busy-tag")) {
          b.parentNode.insertBefore(el("span", "busy-tag", "更新中"), b);
        }
        b.disabled = true;
      }
    });
    // 进度提示常驻，直到出结果（默认 3 秒会被清掉，慢的订阅又变成「没反应」）
    toast(ids.length === 1 ? `正在更新「${subTitle(targets[0])}」…` : `正在更新 ${ids.length} 个订阅…`, 0);

    const results = [];
    for (const id of ids) {
      results.push(await api("/api/subs/update", "POST", { id }));
    }
    await loadState();
    // 用刷新后的数据说话（节点数、用量都是这次更新后的）
    const fresh = STATE?.config?.outlet?.subscriptions || [];
    const lines = ids.map((id, i) => {
      const sub = fresh.find((s) => subKey(s) === id) || targets[i];
      return subResultLine(sub, results[i]);
    });
    const fails = lines.filter((l) => l.includes("失败"));
    if (!fails.length) {
      toast(lines.length === 1 ? lines[0] : `已更新 ${lines.length} 个订阅`);
    } else if (fails.length === lines.length) {
      toast(lines.join("；"), 6000);
    } else {
      toast(`更新 ${lines.length - fails.length} 个成功；${fails.join("；")}`, 6000);
    }
  } catch (e) {
    toast(`更新失败：${e.message}`, 5000);
    await loadState();
  } finally {
    subBusy = false;
    cards.forEach((c) => {
      c.classList.remove("busy");
      c.querySelectorAll(".busy-tag").forEach((n) => n.remove());
      const b = c.querySelector(".dots");
      if (b) b.disabled = false;
    });
  }
}

async function toggleSub(s) {
  const on = s.enabled === false;
  const d = await api("/api/subs/update", "POST", { id: s.id || subKey(s), enabled: on });
  toast(on ? "已启用" : "已停用");
  await loadState();
}

async function editSub(s) {
  const local = (s.url || "").startsWith("local://");
  const v = await Modal.ask({
    title: "修改订阅",
    note: local ? "这条是本地粘贴的订阅，内容存在本地，不能改地址。" : "",
    fields: [
      { key: "name", label: "备注名", value: s.name || "", placeholder: "留空则显示地址" },
      { key: "url", label: "订阅地址", value: local ? "" : (s.url || ""),
        placeholder: local ? "本地粘贴" : "https://…", readonly: local },
      { key: "ua", label: "User-Agent", value: s.ua || "", placeholder: "clash.meta" },
      { key: "proxy", label: "通过代理拉取（可选）", value: s.proxy || "", placeholder: "socks5://… 或 http://…" },
    ],
    okText: "保存",
  });
  if (!v) return;
  const body = { id: s.id || subKey(s), ua: v.ua || "clash.meta", proxy: v.proxy || "" };
  if (v.name) body.name = v.name;
  if (v.url && v.url !== s.url) body.url = v.url;
  const d = await api("/api/subs/update", "POST", body);
  // 改了地址会顺手重拉这一条：拉不到也要说清楚，别只回「已保存」
  toast(d.sub_ok === false ? `已保存，但拉取失败：${d.sub_message || "未知原因"}` : "已保存");
  await loadState();
}

async function shareSub(s) {
  const nodeNames = (STATE?.config?.outlet?.sb_nodes || [])
    .filter((n) => (n.source || "") === (s.name || subTitle(s)))
    .map((n) => n.name);
  if (!nodeNames.length) { toast("这个订阅下没有可用节点"); return; }
  await shareNodes(nodeNames);
}

/* 字节转成人看的单位（机场给的是字节，1 GiB = 1024^3） */
function bytes2(n) {
  n = Number(n) || 0;
  const u = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${i === 0 ? Math.round(n) : n.toFixed(2)} ${u[i]}`;
}

function fmtExpire(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return "";
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/* 用量：已用 X / 总量，到期时间单独一段（窄卡片会自然折成两行） */
function usageLine(u) {
  const used = `已用 ${bytes2(u.used)} / ${u.total > 0 ? bytes2(u.total) : "不限量"}`;
  const exp = fmtExpire(u.expire);
  return exp ? `${used}　到期 ${exp}` : used;
}

function usageBar(u, cls) {
  const bar = el("div", cls || "sc-bar");
  const pct = Math.min(100, Math.max(1, Math.round((u.used / u.total) * 100)));
  const fill = el("i", "");
  fill.style.width = `${pct}%`;
  bar.appendChild(fill);
  bar.title = `已用 ${pct}%（${bytes2(u.used)} / ${bytes2(u.total)}）`;
  return bar;
}

/* 「2026-10-31 15:50」-> 「7 小时前」：卡片窄，相对时间比完整时间戳省地方 */
function relTime(s) {
  if (!s) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(String(s).trim());
  if (!m) return String(s);
  const then = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]).getTime();
  const mins = Math.floor((Date.now() - then) / 60000);
  if (mins < 0) return `${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
  if (mins < 1) return "刚刚";
  if (mins < 60) return `${mins} 分钟前`;
  if (mins < 60 * 24) return `${Math.floor(mins / 60)} 小时前`;
  if (mins < 60 * 24 * 7) return `${Math.floor(mins / 1440)} 天前`;
  return `${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
}

function hasUsage(s) {
  const u = s.usage || {};
  return u.total > 0 || u.used > 0 || u.expire > 0;
}

function renderSubList(subs) {
  const box = $("#subList");
  clear(box);
  // 清掉已被删除的选择
  const alive = new Set(subs.map(subKey));
  Array.from(subSel).forEach((k) => { if (!alive.has(k)) subSel.delete(k); });

  if (!subs.length) {
    box.appendChild(el("div", "hint", "还没有订阅源。点下方「＋ 新增订阅 / 粘贴导入」添加，可添加多个并存。"));
    syncSubSel();
    return;
  }

  subs.forEach((s) => {
    const key = subKey(s);
    const card = el("div", "subcard");
    card.dataset.key = key;   // 更新时按这个找到卡片，给「更新中」的状态
    if (s.enabled === false) card.classList.add("off");

    const head = el("div", "sc-head");
    const chk = el("input");
    chk.type = "checkbox";
    chk.checked = subSel.has(key);
    chk.addEventListener("change", () => {
      if (chk.checked) subSel.add(key); else subSel.delete(key);
      syncSubSel();
    });
    head.appendChild(chk);
    head.appendChild(el("div", "sc-name", subTitle(s)));
    head.appendChild(dotsBtn((btn) => Pop.toggle(btn, [
      { label: "更新", onClick: () => safe(() => updateSubs([s.id || key]), "更新订阅") },
      { label: "修改", onClick: () => safe(() => editSub(s), "修改订阅") },
      { label: "分享全部节点", onClick: () => safe(() => shareSub(s), "分享订阅") },
      { label: s.enabled === false ? "启用此订阅" : "停用此订阅", onClick: () => safe(() => toggleSub(s), "切换订阅") },
      { sep: true },
      { label: "删除", danger: true, onClick: () => safe(() => delSubs([key], subTitle(s)), "删除订阅") },
    ])));
    head.appendChild(icoDel("删除此订阅", () => safe(() => delSubs([key], subTitle(s)), "删除订阅")));
    card.appendChild(head);

    const meta = el("div", "sc-meta");
    meta.appendChild(el("span", "sc-badge", `节点数: ${s.count || 0}`));
    meta.appendChild(el("span", "sc-badge mono", s.updated_at || "未更新"));
    if (s.enabled === false) meta.appendChild(pill("已停用", "bad"));
    card.appendChild(meta);

    // 流量用量：机场在订阅响应头里给了才显示，没给就整行不出现
    if (hasUsage(s)) {
      card.appendChild(el("div", "sc-use mono", usageLine(s.usage)));
      if (s.usage.total > 0) card.appendChild(usageBar(s.usage));
    }

    const url = (s.url || "").startsWith("local://") ? "本地粘贴" : (s.url || "");
    card.appendChild(el("div", "sc-url", url));
    box.appendChild(card);
  });
  syncSubSel();
}

/* ---------------- 节点管理（v2rayA 风格：网格卡片 + 来源分组 + 搜索 + 延迟） ---------------- */
const nodeState = {
  source: "",        // 当前来源筛选（""=全部）
  q: "",             // 搜索关键词
  view: "grid",     // grid | list
  lastSeq: 0,       // 日志增量拉取游标
};
let nodeCache = [];   // 当前节点全集（含 source）
let delayMap = {};    // name -> 最近一次测速结果（排序用）
let delayTcp = {};    // name -> TCP 握手结果
let delayHttp = {};   // name -> 自定义网址实测结果

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

const nodeSel = new Set();

function nodeDisSet() { return new Set(STATE?.config?.outlet?.sb_disabled || []); }
function nodeRemoved() { return STATE?.config?.outlet?.sb_removed || []; }

function syncSelCount() {
  const n = nodeSel.size;
  const c = $("#nodeSelCount");
  if (c) c.textContent = nodeCache.length ? `已选 ${n} 项 / 共 ${nodeCache.length} 个` : "";
  ["#btnNodeDelSel", "#btnNodeShareSel", "#btnNodeEnableSel", "#btnNodeDisableSel"].forEach((s) => {
    const b = $(s);
    if (b) b.disabled = n === 0;
  });
  const all = $("#nodeSelAll");
  if (all) {
    const visible = filterNodes().length;
    all.checked = visible > 0 && n >= visible;
    all.indeterminate = n > 0 && n < visible;
  }
  const rb = $("#btnNodeRestore");
  if (rb) {
    const cnt = nodeRemoved().length;
    rb.hidden = cnt === 0;
    rb.textContent = `恢复已删除 (${cnt})`;
  }
}

async function setDisabled(names, disabled) {
  if (!names.length) { toast("请先选择节点"); return; }
  const cur = nodeDisSet();
  names.forEach((x) => { if (disabled) cur.add(x); else cur.delete(x); });
  await api("/api/nodes", "POST", { disabled: Array.from(cur) });
  toast(disabled ? `已停用 ${names.length} 个节点` : `已启用 ${names.length} 个节点`);
  await loadState();
}

// 卡片上那颗「启用 / 停用」：点一下直接切
function toggleNodeState(n) {
  return setDisabled([n.name], !nodeDisSet().has(n.name));
}

// 点卡片 = 把出口切到这个节点；原本停用的会被一起启用（一次请求搞定）
async function setActiveNode(n) {
  const cur = nodeDisSet();
  const wasOff = cur.has(n.name);
  cur.delete(n.name);
  const r = await api("/api/nodes", "POST", { node: n.name, disabled: Array.from(cur) });
  if (r && r.ok === false) { toast(r.message || "切换失败"); return; }
  toast(wasOff ? `已启用并切到「${n.name}」` : `出口已切到「${n.name}」`);
  await loadState();
}

async function delNodes(names) {
  if (!names.length) { toast("请先选择节点"); return; }
  const tip = names.length === 1
    ? `确认删除节点「${names[0]}」？\n订阅里仍保留，可用「恢复已删除」找回。`
    : `确认删除选中的 ${names.length} 个节点？\n订阅里仍保留，可用「恢复已删除」找回。`;
  if (!confirm(tip)) return;
  await api("/api/nodes", "POST", { delete: names });
  names.forEach((x) => nodeSel.delete(x));
  toast(`已删除 ${names.length} 个节点`);
  await loadState();
}

async function restoreNodes(back) {
  await api("/api/nodes", "POST", { restore: back });
  toast(`已恢复 ${back.length} 个节点`);
  await loadState();
}

async function renameNode(n) {
  const v = await Modal.ask({
    title: "修改节点名称",
    note: "只改本地显示名与配置里的 tag，不影响服务器地址。",
    value: n.name,
    placeholder: "新名称",
    okText: "保存",
  });
  if (!v) return;
  const to = (v.value || "").trim();
  if (!to || to === n.name) return;
  await api("/api/nodes", "POST", { rename: { from: n.name, to } });
  nodeSel.delete(n.name);
  toast("已重命名");
  await loadState();
}

/* ---------------- 节点测速：HTTP 实测（真的走一遍） vs TCP 握手 ---------------- */
/* 交互：先在「目标」菜单里选对象（只选不跑），再点「测速」按钮才执行 */
const TEST_TARGET_KEY = "upx_test_target";
const TEST_TARGETS = [
  { label: "Google", url: "https://www.gstatic.com/generate_204" },
  { label: "GitHub", url: "https://github.com/" },
  { label: "Cloudflare", url: "https://cloudflare.com/cdn-cgi/trace" },
  { label: "TCP 握手（只看端口）", url: "", tcp: true },
];

function shortUrl(u) { return (u || "").replace(/^https?:\/\//, "").replace(/\/$/, ""); }

function testTarget() {
  let t = null;
  try { t = JSON.parse(localStorage.getItem(TEST_TARGET_KEY) || "null"); } catch (e) { t = null; }
  // 默认目标是 TCP 握手：进页面就能看到每个节点的端口延迟，不必等一次 HTTP 实测。
  // 用户显式挑了 Google/GitHub/自定义地址才按那个来（存在 localStorage 里）。
  const fallback = () => Object.assign({}, TEST_TARGETS[3]);
  if (!t || typeof t !== "object") return fallback();
  if (t.tcp) return { label: t.label || TEST_TARGETS[3].label, url: "", tcp: true };
  return t.url ? { label: t.label || shortUrl(t.url), url: t.url } : fallback();
}

function setTestTarget(t) {
  try { localStorage.setItem(TEST_TARGET_KEY, JSON.stringify(t)); } catch (e) { /* 隐私模式忽略 */ }
}

/* 把当前目标同步到工具栏两个按钮上 */
function syncTestButtons() {
  const t = testTarget();
  const sel = $("#btnTestTarget");
  if (sel) {
    sel.textContent = `目标：${t.tcp ? "TCP 握手" : t.label} ▾`;
    sel.title = t.tcp
      ? "当前只测端口能否连上（不验证能否出网）· 点这里换目标"
      : `当前目标 ${t.url} · 点这里换`;
  }
  const run = $("#btnNodeTest");
  if (run) {
    run.title = (t.tcp ? "对全部节点做 TCP 握手测速" : `经每个节点真实访问 ${t.url}`)
      + (testSortOn() ? "，测完按延迟重排并保存顺序" : "");
  }
  // 仪表板「节点延迟」卡片的测速按钮跟着目标走，别让人猜这次测的是哪种
  const dash = $("#btnDashTest");
  if (dash && !dash.disabled) {
    dash.textContent = t.tcp ? "测速 TCP" : "测速 HTTP";
    dash.title = t.tcp
      ? "对本组节点做 TCP 握手测速（3 秒超时）"
      : `经每个节点真实访问 ${t.url}`;
  }
}

/* 延迟结果的展示口径：HTTP 实测和 TCP 握手不能混为一谈 */
function delayKind(r) {
  if (!r || !r.reachable) return "bad";
  if (r.mode === "tcp") return r.ms < 300 ? "ok" : "warn";
  return r.ms < 800 ? "ok" : (r.ms < 2000 ? "warn" : "bad");
}

function delayText(r) {
  if (!r) return "—";
  if (!r.reachable) return r.mode === "http" ? "失败" : "TIMEOUT";
  if (r.ms === null || r.ms === undefined) {
    return r.mode === "http" && r.code ? `HTTP ${r.code}` : "OK";
  }
  return `${r.mode === "http" ? "HTTP " : "TCP "}${r.ms}ms`;
}

function delayTitle(r) {
  if (!r) return "";
  const head = r.mode === "http"
    ? `经该节点访问 ${r.url || "测试地址"}${r.code ? `（HTTP ${r.code}）` : ""} 的实际耗时`
    : "TCP 握手耗时（只连节点端口，不代表能出网）";
  return r.error ? `${head}\n失败原因：${r.error}` : head;
}

/* 两种延迟分开存：TCP 握手只连节点端口，HTTP 是真经节点访问网址，
   同一台节点两个数常常差一个量级，混成一个数会误导人。 */
function latOf(name, kind) {
  return (kind === "http" ? delayHttp[name] : delayTcp[name]) || null;
}

/* 卡片/菜单里的短格式：TCP 149ms / HTTP 320ms / 超时 / — */
function latShort(r, kind) {
  const tag = kind === "http" ? "HTTP" : "TCP";
  if (!r) return `${tag} —`;
  if (!r.reachable) return `${tag} 超时`;
  if (r.ms === null || r.ms === undefined) {
    return `${tag} ${r.code ? `HTTP ${r.code}` : "OK"}`;
  }
  return `${tag} ${r.ms}ms`;
}

/* 菜单里紧凑一点：不带前缀，前缀在行首统一给 */
function msShort(r) {
  if (!r) return "—";
  if (!r.reachable) return "超时";
  if (r.ms === null || r.ms === undefined) return r.mode === "http" && r.code ? `HTTP ${r.code}` : "OK";
  return `${r.ms}ms`;
}

/* 测速排序：延迟低的在前，超时/失败/没测过的垫底。
   排序结果会存到后端（outlet.sb_order），重启、重新同步订阅后顺序不变。 */
const TEST_SORT_KEY = "upx_test_sort";

function testSortOn() {
  try { return localStorage.getItem(TEST_SORT_KEY) !== "0"; } catch (e) { return true; }
}

function setTestSort(on) {
  try { localStorage.setItem(TEST_SORT_KEY, on ? "1" : "0"); } catch (e) { /* 隐私模式忽略 */ }
}

// 名次：0=有耗时 1=通但没耗时 2=超时/失败 3=没有结果
function delayRank(r) {
  if (!r) return 3;
  if (!r.reachable) return 2;
  return (r.ms === null || r.ms === undefined) ? 1 : 0;
}

function sortNodesByDelay() {
  const before = nodeCache.map((n) => n.name);
  if (!before.length) return false;
  const idx = new Map(before.map((n, i) => [n, i]));
  nodeCache = nodeCache.slice().sort((a, b) => {
    const ra = delayMap[a.name] || null;
    const rb = delayMap[b.name] || null;
    const ka = delayRank(ra), kb = delayRank(rb);
    if (ka !== kb) return ka - kb;              // 有耗时的整体排在失败前面
    if (ka === 0 && ra.ms !== rb.ms) return ra.ms - rb.ms;  // 同组内按延迟升序
    return idx.get(a.name) - idx.get(b.name);   // 其余保持原有相对顺序
  });
  const after = nodeCache.map((n) => n.name);
  if (after.join("\u0000") === before.join("\u0000")) return false;
  // 同步进 STATE，renderNodes 会从 STATE 重建 nodeCache
  if (STATE?.config?.outlet) STATE.config.outlet.sb_nodes = nodeCache.slice();
  return true;
}

async function saveNodeOrder() {
  const order = nodeCache.map((n) => n.name);
  if (!order.length) return false;
  try {
    await api("/api/nodes", "POST", { order: order });
    return true;
  } catch (e) {
    toast(`排序结果保存失败：${e.message}`);
    return false;
  }
}

async function runNodeTest(names, mode, url, opts) {
  const opt = opts || {};
  const one = names && names.length === 1;
  const t = testTarget();
  if (!mode) mode = t.tcp ? "tcp" : "http";
  if (mode === "http" && !url) url = t.tcp ? TEST_TARGETS[0].url : t.url;
  const tgt = mode === "http" ? { label: shortUrl(url), url: url } : null;

  const body = { mode: mode, timeout: mode === "tcp" ? 3000 : 5000 };
  if (opt.quiet) body.quiet = true;      // 界面自己发起的补测，别写进日志
  if (tgt) body.url = tgt.url;
  if (names) body.nodes = nodeCache.filter((x) => names.includes(x.name));

  // 结果只落在胶囊上；进度和汇总各用一条 toast，页面里不再堆文字
  const btn = $("#btnNodeTest");
  const label = btn ? btn.textContent : "";
  if (btn && !names) { btn.disabled = true; btn.textContent = "测速中…"; }
  if (!opt.silent) toast(mode === "tcp"
    ? `正在握手：${one ? names[0] : "全部节点"}…`
    : `正在经节点访问 ${tgt.label}：${one ? names[0] : "全部节点"}…`);

  let d;
  try {
    d = await api("/api/subs/test", "POST", body);
  } finally {
    if (btn && !names) { btn.disabled = false; btn.textContent = label; }
  }

  const res = d.results || [];
  if (!names) {
    // 全量测速：只清本次这一种结果，另一种保留着（两种要同时显示）
    if (mode === "tcp") { delayMap = {}; delayTcp = {}; } else { delayHttp = {}; }
  }
  res.forEach((r) => {
    delayMap[r.tag] = r;
    if (r.mode === "http") delayHttp[r.tag] = r; else delayTcp[r.tag] = r;
  });

  // 全部测速完成 → 按延迟重排并落盘；单节点测速不动顺序，免得卡片乱跳
  // （首页测「当前分组」时传 opts.sort，顺序照样跟着最新结果走）
  let sorted = false, saved = false;
  if ((!names || opt.sort) && res.length && testSortOn()) sorted = sortNodesByDelay();
  renderNodes();
  Dash.onNodes();          // 首页「代理组 / 节点延迟」两张卡跟着刷新
  if (sorted) saved = await saveNodeOrder();
  // 返回原始响应：静默补测要靠 d.mode / d.note 判断是不是被降级了
  if (opt.silent) return d;

  const up = res.filter((r) => r.reachable);
  if (one) {
    const r = res[0];
    toast(!r ? "测速失败"
      : r.reachable ? `${names[0]}：${r.ms}ms（${d.mode === "http" ? "HTTP" : "TCP"}）`
        : `${names[0]}：${r.error || "不可用"}`);
    return;
  }
  const best = up.length ? Math.min.apply(null, up.map((r) => r.ms)) : null;
  const head = d.mode === "http"
    ? `实测 ${shortUrl(d.url || (tgt && tgt.url))}`
    : (mode === "tcp" ? "TCP 握手" : "本机无 Docker，已降级为 TCP 握手");
  toast(`${head}：可用 ${up.length}/${res.length}` + (best !== null ? `，最快 ${best}ms` : "")
    + (sorted ? (saved ? "，已按延迟重排" : "，排序未保存") : ""));
}

/* 进页面自动刷一轮 TCP 握手延迟：标签上立刻有数字，不必先点测速。
   只测「当前代理组」——首页「节点延迟」卡片展示的就是这一组；
   和手动测速一样按延迟重排并保存，顺序永远反映最近一次结果。 */
function autoTcpProbe() {
  const list = groupNodes();
  if (!list.length) return;
  runNodeTest(list.map((n) => n.name), "tcp", null, { silent: true, sort: true }).catch(() => {});
}

async function editCustomTarget() {
  const cur = testTarget();
  const v = await Modal.ask({
    title: "自定义测速地址",
    note: "填一个能正常返回内容的 https 地址；测的是「经该节点访问它」的往返耗时。选好后点「测速」才开始。",
    value: cur.tcp ? "" : cur.url,
    placeholder: "https://www.google.com/",
    okText: "用它",
  });
  if (!v) return;
  let u = (v.value || "").trim();
  if (!u) return;
  if (!/^https?:\/\//i.test(u)) u = "https://" + u;
  setTestTarget({ label: shortUrl(u), url: u });
  syncTestButtons();
  toast(`目标已设为 ${shortUrl(u)}，点「测速」开始`);
}

function testMenuItems() {
  const cur = testTarget();
  const same = (t) => (t.tcp ? !!cur.tcp : (!cur.tcp && cur.url === t.url));
  const items = TEST_TARGETS.map((t) => ({
    label: `${same(t) ? "✓ " : "　"}${t.label}`,
    onClick: () => { setTestTarget(t); syncTestButtons(); },
  }));
  items.push({ sep: true });
  const custom = !cur.tcp && !TEST_TARGETS.some((t) => t.url === cur.url);
  items.push({
    label: custom ? `✓ 自定义：${shortUrl(cur.url)}` : "自定义地址…",
    onClick: () => safe(editCustomTarget, "测速目标"),
  });
  items.push({ sep: true });
  items.push({
    label: `${testSortOn() ? "✓ " : "　"}测速后按延迟排序`,
    onClick: () => {
      const on = !testSortOn();
      setTestSort(on);
      syncTestButtons();
      toast(on ? "已开启排序：延迟低的排前面，超时的垫底" : "已关闭排序：测速只更新延迟，不改顺序");
    },
  });
  return items;
}

function nodeMenuItems(n) {
  const isOff = nodeDisSet().has(n.name);
  const isDef = STATE?.config?.outlet?.sb_node === n.name;
  return [
    { label: `HTTP 实测（→ ${testTarget().label}）`, onClick: () => safe(() => runNodeTest([n.name], "http"), "测速") },
    { label: "TCP 握手测速", onClick: () => safe(() => runNodeTest([n.name], "tcp"), "测速") },
    { label: "修改名称", onClick: () => safe(() => renameNode(n), "改名") },
    { label: "分享链接", onClick: () => safe(() => shareNodes([n.name]), "分享") },
    { label: isOff ? "启用此节点" : "停用此节点", onClick: () => safe(() => setDisabled([n.name], !isOff), "切换节点") },
    {
      label: isDef ? "取消默认出口" : "设为默认出口",
      onClick: () => safe(async () => {
        await api("/api/nodes", "POST", { node: isDef ? "" : n.name });
        toast(isDef ? "已取消默认出口" : "已设为默认出口");
        await loadState();
      }, "设默认"),
    },
    { sep: true },
    { label: "删除", danger: true, onClick: () => safe(() => delNodes([n.name]), "删除节点") },
  ];
}

function renderNodeGrid() {
  const box = $("#nodeGrid");
  clear(box);
  const items = filterNodes();
  if (!items.length) {
    box.appendChild(el("div", "hint", nodeCache.length ? "无匹配节点" : "暂无节点，请先在上方添加订阅并拉取。"));
    return;
  }
  const dis = nodeDisSet();
  const defaultNode = STATE?.config?.outlet?.sb_node || "";
  items.forEach((n) => {
    const card = el("div", "node-card");
    const disabled = dis.has(n.name);
    if (disabled) card.classList.add("off");
    if (defaultNode === n.name) card.classList.add("active");
    if (nodeSel.has(n.name)) card.classList.add("sel");
    card.setAttribute("data-n", n.name);
    card.title = disabled
      ? "当前是停用状态 · 点卡片会启用它并切为默认出口"
      : "点卡片把出口切到它";

    const head = el("div", "nc-head");
    const chk = el("input");
    chk.type = "checkbox";
    chk.title = "勾选用于批量操作（启用 / 停用 / 删除 / 分享）";
    chk.checked = nodeSel.has(n.name);
    chk.addEventListener("change", () => {
      if (chk.checked) nodeSel.add(n.name); else nodeSel.delete(n.name);
      card.classList.toggle("sel", chk.checked);
      syncSelCount();
    });
    head.appendChild(chk);
    head.appendChild(el("div", "nc-name", n.name));
    if (defaultNode === n.name) head.appendChild(el("span", "nc-def", "默认"));
    head.appendChild(dotsBtn((btn) => Pop.toggle(btn, nodeMenuItems(n))));
    head.appendChild(icoDel("删除此节点", () => safe(() => delNodes([n.name]), "删除节点")));
    card.appendChild(head);

    const meta = el("div", "nc-meta");
    meta.appendChild(el("span", "nc-type", n.type));
    meta.appendChild(el("span", "nc-src", n.source || "本地"));
    // 启用 / 停用状态：直接显示在卡片上，点一下立刻切换
    const st = el("span", `nc-state ${disabled ? "bad" : "ok"}`, disabled ? "停用" : "启用");
    st.title = disabled ? "点一下启用这个节点" : "点一下停用这个节点";
    st.addEventListener("click", (e) => {
      e.stopPropagation();
      safe(() => toggleNodeState(n), "切换节点");
    });
    meta.appendChild(st);
    card.appendChild(meta);
    card.appendChild(el("div", "nc-addr", `${n.server}:${n.port}`));

    const d = delayMap[n.name];
    const delayEl = el("span", d ? `nc-delay ${delayKind(d)}` : "nc-delay", delayText(d));
    delayEl.title = delayTitle(d);
    card.appendChild(delayEl);

    // 点卡片 = 把出口切到它（顺带启用）；批量勾选交给左边的小方框
    card.addEventListener("click", (e) => {
      if (e.target.closest("input, button, .dots, .nc-state")) return;
      safe(() => setActiveNode(n), "切换出口");
    });
    box.appendChild(card);
  });
}

function renderNodeTable(nodes, sbNode) {
  const tb = $("#nodeTbody");
  clear(tb);
  const items = filterNodes();
  if (!items.length) {
    const td = el("td", "", nodeCache.length ? "无匹配节点" : "暂无节点，请先在上方添加订阅并拉取。");
    td.setAttribute("colspan", "7");
    const tr = el("tr");
    tr.appendChild(td);
    tb.appendChild(tr);
    return;
  }
  const dis = nodeDisSet();
  items.forEach((n) => {
    const row = el("tr");
    row.setAttribute("data-n", n.name);
    if (dis.has(n.name)) row.classList.add("off");

    const selTd = el("td");
    const chk = el("input");
    chk.type = "checkbox";
    chk.checked = nodeSel.has(n.name);
    chk.addEventListener("change", () => {
      if (chk.checked) nodeSel.add(n.name); else nodeSel.delete(n.name);
      syncSelCount();
    });
    selTd.appendChild(chk);
    row.appendChild(selTd);

    const nameTd = el("td");
    const nameBox = el("div", "row");
    nameBox.appendChild(el("span", "mono", n.name));
    nameBox.appendChild(dotsBtn((btn) => Pop.toggle(btn, nodeMenuItems(n))));
    nameBox.appendChild(icoDel("删除此节点", () => safe(() => delNodes([n.name]), "删除节点")));
    nameTd.appendChild(nameBox);
    row.appendChild(nameTd);

    row.appendChild(el("td")).appendChild(pill(dis.has(n.name) ? "停用" : "启用", dis.has(n.name) ? "bad" : "ok"));
    const stTd = row.lastChild;
    const stPill = stTd.firstChild;
    stPill.style.cursor = "pointer";
    stPill.title = dis.has(n.name) ? "点一下启用这个节点" : "点一下停用这个节点";
    stPill.addEventListener("click", () => safe(() => toggleNodeState(n), "切换节点"));
    row.appendChild(el("td", "", n.source || "本地"));
    row.appendChild(el("td")).appendChild(pill(n.type, "mute"));

    const tTd = el("td", "", "—");
    tTd.setAttribute("data-name", n.name);
    const d = delayMap[n.name];
    if (d) {
      tTd.textContent = "";
      const p = pill(delayText(d), delayKind(d));
      p.title = delayTitle(d);
      tTd.appendChild(p);
    }
    row.appendChild(tTd);

    const defTd = el("td");
    const r = el("input");
    r.type = "radio";
    r.name = "defNode";
    r.checked = (n.name === sbNode);
    r.addEventListener("change", () => safe(async () => {
      await api("/api/nodes", "POST", { node: n.name });
      loadState();
    }, "设默认"));
    defTd.appendChild(r);
    row.appendChild(defTd);
    tb.appendChild(row);
  });
}

function renderNodes() {
  const cfg = STATE?.config?.outlet;
  nodeCache = cfg?.sb_nodes || [];
  // 清掉已被删除/不存在的选择项
  const alive = new Set(nodeCache.map((n) => n.name));
  Array.from(nodeSel).forEach((k) => { if (!alive.has(k)) nodeSel.delete(k); });
  const dis = new Set(cfg?.sb_disabled || []);
  refreshSourceSel();
  syncSelCount();
  // 视图切换：grid 显示网格隐藏表，反之
  // 注意：这里不能写死 display:block —— 会把 .nodegrid 的 display:grid 覆盖掉，
  // 卡片就变成整行铺满，页面被拉得很长。传空串 = 交回给 CSS 决定。
  const gridOn = nodeState.view === "grid";
  $("#nodeGrid").style.display = gridOn ? "" : "none";
  $("#nodeTableWrap").style.display = gridOn ? "none" : "block";
  $("#btnNodeGrid").classList.toggle("on", gridOn);
  $("#btnNodeList").classList.toggle("on", !gridOn);
  if (gridOn) {
    renderNodeGrid();
  }
  // 列表 tbody 始终渲染（隐藏也不影响测速写回）
  renderNodeTable(nodeCache, cfg?.sb_node || "");
  syncTestButtons();
}

$("#nodeSearch").addEventListener("input", (e) => { nodeState.q = e.target.value; if (nodeState.view === "grid") renderNodeGrid(); else renderNodeTable(nodeCache, STATE?.config?.outlet?.sb_node || ""); });
$("#nodeSource").addEventListener("change", (e) => { nodeState.source = e.target.value; renderNodes(); });
$("#btnNodeGrid").addEventListener("click", () => { nodeState.view = "grid"; renderNodes(); });
$("#btnNodeList").addEventListener("click", () => { nodeState.view = "list"; renderNodes(); });

$("#btnTestTarget").addEventListener("click", (e) => {
  Pop.toggle(e.currentTarget, testMenuItems());
});

$("#btnNodeTest").addEventListener("click", () => safe(() => runNodeTest(null), "测速"));

/* ---------------- 批量选择 / 批量操作 ---------------- */
$("#nodeSelAll").addEventListener("change", (e) => {
  const on = e.target.checked;
  nodeSel.clear();
  if (on) filterNodes().forEach((n) => nodeSel.add(n.name));
  renderNodes();
});

$("#subSelAll").addEventListener("change", (e) => {
  const on = e.target.checked;
  subSel.clear();
  if (on) (STATE?.config?.outlet?.subscriptions || []).forEach((s) => subSel.add(subKey(s)));
  renderSubList(STATE?.config?.outlet?.subscriptions || []);
});

$("#btnSubDelSel").addEventListener("click", () => safe(() => delSubs(Array.from(subSel)), "批量删除订阅"));
$("#btnSubUpdSel").addEventListener("click", () => safe(async () => {
  const subs = STATE?.config?.outlet?.subscriptions || [];
  const ids = subs.filter((s) => subSel.has(subKey(s))).map((s) => s.id || subKey(s)).filter(Boolean);
  if (!ids.length) { toast("所选订阅没有可更新的 id"); return; }
  await updateSubs(ids);
}, "批量更新订阅"));

$("#btnNodeDelSel").addEventListener("click", () => safe(() => delNodes(Array.from(nodeSel)), "批量删除节点"));
$("#btnNodeShareSel").addEventListener("click", () => safe(() => shareNodes(Array.from(nodeSel)), "分享节点"));
$("#btnNodeEnableSel").addEventListener("click", () => safe(() => setDisabled(Array.from(nodeSel), false), "启用节点"));
$("#btnNodeDisableSel").addEventListener("click", () => safe(() => setDisabled(Array.from(nodeSel), true), "停用节点"));
$("#btnNodeRestore").addEventListener("click", () => safe(async () => {
  const back = nodeRemoved();
  if (!back.length) { toast("没有已删除的节点"); return; }
  const v = await Modal.ask({
    title: "恢复已删除的节点",
    note: `共 ${back.length} 个。点确定后重新拉取订阅并恢复这些节点。`,
    mode: "text",
    rows: 5,
    value: back.join("\n"),
    okText: "全部恢复",
  });
  if (v === null) return;
  await restoreNodes(back);
}, "恢复节点"));

/* ================= 分流规则编辑器 =================
   规则表是有序的，顺序本身就是配置，所以必须能上移下移。
   文本域直接绑在模型上（input 即写入），这样重排/删除时
   重新渲染列表也不会把用户正在输入的内容弄丢。            */
const Route = (function () {
  let M = null;          // {rules, final, types, actions, finals, nodes, presets, seeded}

  function opt(sel, value, label) {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = label;
    sel.appendChild(o);
    return o;
  }
  function typeOf(key) {
    return (M.types || []).find((t) => t.key === key) || M.types[0];
  }

  function actionLabel(a) {
    if (String(a).startsWith("node:")) return `节点：${String(a).slice(5)}`;
    const f = (M.actions || []).find((x) => x.key === a);
    return f ? f.label : a;
  }

  function refreshTun(outlet) {
    $("#sbTun").checked = !!outlet.sb_tun;
    $("#inAutoUpdate").value = String(outlet.sb_auto_update || 0);
  }

  async function load() {
    const d = await api("/api/route");
    M = d;
    render();
  }

  function render() {
    // 兜底动作
    const fs = $("#selRouteFinal");
    clear(fs);
    (M.finals || []).forEach((f) => opt(fs, f.key, f.label));
    fs.value = M.final || "direct";
    $("#routeFinalHint").textContent = M.final === "proxy"
      ? "白名单式：只有命中的规则走直连/拦截，其余全部走代理。"
      : "黑名单式：只有命中的规则走代理，其余全部直连（国内站点不受影响）。";

    const box = $("#ruleList");
    clear(box);
    const rules = M.rules || [];
    $("#routeEmpty").style.display = rules.length ? "none" : "";
    $("#routeSeeded").style.display = M.seeded ? "" : "none";
    rules.forEach((r, i) => box.appendChild(row(r, i)));
  }

  function row(r, i) {
    const wrap = el("div", "ruleitem" + (r.enabled === false ? " off" : ""));
    if (r._open) wrap.classList.add("open");
    const head = el("div", "rulehead");

    // 启用开关
    const on = document.createElement("input");
    on.type = "checkbox";
    on.checked = r.enabled !== false;
    on.title = "停用后规则保留，但不参与匹配";
    on.addEventListener("change", () => {
      r.enabled = on.checked;
      wrap.classList.toggle("off", !on.checked);
    });
    const onWrap = el("label", "chk rule-on");
    onWrap.appendChild(on);
    head.appendChild(onWrap);

    head.appendChild(el("span", "rule-no", `#${i + 1}`));

    // 匹配类型
    const ms = document.createElement("select");
    ms.className = "rule-match";
    (M.types || []).forEach((t) => opt(ms, t.key, t.label));
    ms.value = r.match;
    ms.title = typeOf(r.match).hint || "";
    head.appendChild(ms);

    head.appendChild(el("span", "rule-arrow", "→"));

    // 动作
    const as = document.createElement("select");
    as.className = "rule-action";
    (M.actions || []).forEach((a) => opt(as, a.key, a.label));
    if ((M.nodes || []).length) {
      const g = document.createElement("optgroup");
      g.label = "指定节点";
      (M.nodes || []).forEach((n) => {
        const o = document.createElement("option");
        o.value = `node:${n}`;
        o.textContent = `节点：${n}`;
        g.appendChild(o);
      });
      as.appendChild(g);
    }
    // 动作对应的节点可能已被删掉，补一个占位项免得 select 显示成空
    if (String(r.action).startsWith("node:") && !(M.nodes || []).includes(r.action.slice(5))) {
      opt(as, r.action, actionLabel(r.action) + "（已失效）");
    }
    as.value = r.action;
    as.addEventListener("change", () => { r.action = as.value; });
    head.appendChild(as);

    const cnt = el("span", "rule-count", "");
    head.appendChild(cnt);
    const spacer = el("span", "grow rule-gap");
    spacer.title = "点空白处展开 / 收起";
    head.appendChild(spacer);

    const mk = (txt, title, cls, fn) => {
      const b = el("button", "ico " + (cls || ""), txt);
      b.type = "button";
      b.title = title;
      b.addEventListener("click", fn);
      return b;
    };
    head.appendChild(mk("↑", "上移（越靠前优先级越高）", "", () => move(i, -1)));
    head.appendChild(mk("↓", "下移", "", () => move(i, 1)));
    head.appendChild(mk("✕", "删除这条规则", "danger", () => {
      M.rules.splice(i, 1);
      render();
    }));
    const fold = mk(r._open ? "▲" : "▼", "展开 / 收起内容", "fold-btn", () => {
      r._open = !wrap.classList.contains("open");
      wrap.classList.toggle("open", r._open);
      fold.textContent = r._open ? "▲" : "▼";
      if (r._open) {
        const ta2 = wrap.querySelector(".rule-vals");
        if (ta2) ta2.focus();
      }
    });
    head.appendChild(fold);
    spacer.addEventListener("click", () => fold.click());
    wrap.appendChild(head);

    // 收起时的一行摘要：不展开也知道这条写了什么
    const sum = el("div", "rule-sum");
    sum.title = "点一下展开编辑";
    sum.addEventListener("click", () => fold.click());
    wrap.appendChild(sum);

    const body = el("div", "rule-body");
    // 值：一行一个
    const ta = document.createElement("textarea");
    ta.className = "rule-vals";
    ta.value = (r.values || []).join("\n");
    ta.spellcheck = false;
    ta.placeholder = typeOf(r.match).ph || "";
    const autosize = () => {
      ta.rows = Math.min(8, Math.max(2, ta.value.split("\n").length));
    };
    autosize();
    const sync = () => {
      r.values = ta.value.split("\n").map((s) => s.trim()).filter(Boolean);
      cnt.textContent = `${r.values.length} 项`;
      const vals = r.values;
      sum.textContent = vals.length
        ? ` ${vals.slice(0, 3).join("、")}${vals.length > 3 ? ` … 共 ${vals.length} 项` : ""}`
        : " （还没有填内容，点这里展开）";
      autosize();
    };
    ta.addEventListener("input", sync);
    sync();
    body.appendChild(ta);

    const note = el("div", "rule-note");
    const hintText = () => {
      const t = typeOf(r.match);
      note.textContent = `${t.hint}${r.note ? " · " + r.note : ""}`;
      note.title = note.textContent;
    };
    hintText();
    ms.addEventListener("change", () => {
      r.match = ms.value;
      ms.title = typeOf(r.match).hint || "";
      ta.placeholder = typeOf(r.match).ph || "";
      hintText();
    });
    body.appendChild(note);
    wrap.appendChild(body);
    return wrap;
  }

  function move(i, d) {
    const j = i + d;
    if (j < 0 || j >= M.rules.length) return;
    const t = M.rules[i];
    M.rules[i] = M.rules[j];
    M.rules[j] = t;
    render();
  }

  function ruleKey(r) {
    return `${r.match}|${r.action}|${(r.values || []).join(",")}`;
  }

  function pushRule(rule) {
    M.rules.push(Object.assign({
      id: "", enabled: true, match: "domain_suffix", values: [], action: "proxy",
      note: "", _open: true,
    }, rule || {}));
    M.seeded = false;
  }

  function add(rule) {
    if (!M) return;
    pushRule(rule);
    render();
    const items = $$("#ruleList .ruleitem");
    const last = items[items.length - 1];
    if (last) {
      last.scrollIntoView({ block: "nearest", behavior: "smooth" });
      const ta = last.querySelector(".rule-vals");
      if (ta) ta.focus();
    }
  }

  function payload() {
    if (!M) throw new Error("规则还没加载完，稍等一下");
    return { rules: M.rules || [], final: M.final || "direct" };
  }

  async function test() {
    const t = $("#inRouteTest").value.trim();
    if (!t) { toast("先填一个域名"); return; }
    const out = $("#routeTestOut");
    const d = await api("/api/route/test", "POST", Object.assign({ target: t }, payload()));
    clear(out);
    out.appendChild(pill(d.action_text, d.action === "direct" ? "" : d.action === "block" ? "bad" : "ok"));
    out.appendChild(el("span", "", " " + d.message));
    if (d.hit) {
      // 命中的那条如果是收起的，自动展开，让用户看见它到底写了什么
      const target = (M.rules || [])[d.index - 1];
      if (target && !target._open) {
        target._open = true;
        render();
      }
      const node = $$("#ruleList .ruleitem")[d.index - 1];
      if (node) {
        document.querySelectorAll("#ruleList .ruleitem.hit").forEach((n) => n.classList.remove("hit"));
        node.classList.add("hit");
        node.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    }
  }

  async function preview() {
    const out = $("#routeOut");
    const d = await api("/api/route", "POST", Object.assign({ preview: true }, payload()));
    clear(out);
    (d.preview || []).forEach((line) => out.appendChild(el("div", "mono", line)));
    out.appendChild(el("div", "hint", `按 sing-box ${d.version || ""} 的语法生成，实际下发内容：`));
    out.appendChild(pre(JSON.stringify(d.route, null, 2)));
  }

  async function save(restart) {
    const out = $("#routeOut");
    const d = await api("/api/route", "POST", Object.assign({ restart: !!restart }, payload()));
    // TUN / 自动更新这两项跟规则无关，顺手一起存
    await api("/api/settings", "POST", {
      outlet: {
        sb_tun: $("#sbTun").checked,
        sb_auto_update: parseInt($("#inAutoUpdate").value || "0", 10) || 0,
      },
    });
    clear(out);
    out.appendChild(pill("已保存", "ok"));
    out.appendChild(el("div", "", d.message || ""));
    (d.warnings || []).forEach((w) => out.appendChild(el("div", "hint", "注意：" + w)));
    toast(d.message || "分流规则已保存");
    M.seeded = false;
    $("#routeSeeded").style.display = "none";
    await load();
  }

  let bound = false;

  /* 仪表板上的「分流模式」单选走这里：只改兜底动作，规则表原样保留 */
  async function setFinal(final) {
    const want = final === "proxy" ? "proxy" : "direct";
    if (!M) await load();
    M.final = want;
    const fs = $("#selRouteFinal");
    if (fs) fs.value = want;
    await save(true);   // 保存并重启，立刻生效
  }

  function boot(outlet) {
    refreshTun(outlet);
    if (bound) return;
    bound = true;
    load().catch((e) => toast("读取分流规则失败：" + e.message));

    $("#btnRuleAdd").addEventListener("click", () => add());
    $("#btnRouteTest").addEventListener("click", () => safe(test, "测命中"));
    $("#inRouteTest").addEventListener("keydown", (e) => {
      if (e.key === "Enter") safe(test, "测命中");
    });
    $("#btnRoutePreview").addEventListener("click", () => safe(preview, "预览配置"));
    $("#btnRouteSave").addEventListener("click", () => safe(() => save(false), "保存规则"));
    $("#btnRouteSaveApply").addEventListener("click", () => safe(() => save(true), "保存并重启"));
    $("#selRouteFinal").addEventListener("change", (e) => {
      M.final = e.target.value;
      render();
    });

    $("#btnRulePreset").addEventListener("click", (e) => {
      const items = (M.presets || []).map((p) => ({
        label: `${p.label}（${p.count} 项）`,
        onClick: () => {
          // 重复套同一个模板不该越叠越多
          const seen = new Set(M.rules.map(ruleKey));
          let added = 0;
          let skipped = 0;
          p.rules.forEach((r) => {
            const copy = JSON.parse(JSON.stringify(r));
            if (seen.has(ruleKey(copy))) { skipped += 1; return; }
            pushRule(copy);
            seen.add(ruleKey(copy));
            added += 1;
          });
          render();
          if (!added) { toast(`模板「${p.label}」里的规则都已经有了`); return; }
          toast(`已追加 ${added} 条规则` + (skipped ? `（${skipped} 条重复已跳过）` : ""));
        },
      }));
      if (items.length) items.push({ sep: true });
      items.push({
        label: "清空全部规则",
        danger: true,
        onClick: () => { M.rules = []; M.seeded = false; render(); },
      });
      Pop.open(e.currentTarget, items);
    });
  }

  return { boot, load, get: () => M, setFinal };
})();

/* 新增订阅表单默认收起，省纵向空间 */
const subAddWrap = $("#subAddWrap");
function setAddOpen(on) {
  subAddWrap.classList.toggle("open", !!on);
  const b = $("#btnSubToggleAdd");
  if (b) b.textContent = on ? "收起新增表单" : "＋ 新增订阅 / 粘贴导入";
}
if ($("#btnSubToggleAdd")) {
  $("#btnSubToggleAdd").addEventListener("click", () => setAddOpen(!subAddWrap.classList.contains("open")));
}
if ($("#btnSubClose")) $("#btnSubClose").addEventListener("click", () => setAddOpen(false));

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
  if (subBusy) { toast("正在更新，等这一轮跑完"); return; }
  const out = $("#subOut"); clear(out); out.appendChild(el("div", "hint", "正在更新全部订阅…"));
  const cards = Array.from(document.querySelectorAll(".subcard"));
  subBusy = true;
  cards.forEach((c) => c.classList.add("busy"));
  toast("正在更新全部订阅…", 0);
  try {
    const d = await api("/api/subs/update", "POST", {});
    clear(out);
    out.appendChild(pill(d.ok ? "完成" : "失败", d.ok ? "ok" : "bad"));
    out.appendChild(el("div", "", d.message));
    // 逐条列出结果：哪条成功、哪条挂在哪，比一句合并摘要有用
    (d.summary || []).forEach((s) => {
      const extra = s.ok && s.usage && s.usage.total
        ? `（已用 ${bytes2(s.usage.used)} / ${bytes2(s.usage.total)}）` : "";
      out.appendChild(el("div", "mono", `${s.ok ? "✓" : "✕"} ${s.name || s.url}：${s.message}${extra}`));
    });
    toast(d.ok ? "已更新全部订阅" : "部分订阅更新失败", d.ok ? 3000 : 6000);
    await loadState();
  } finally {
    subBusy = false;
    cards.forEach((c) => c.classList.remove("busy"));
  }
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

/* 分流规则编辑器自己在 Route.boot() 里挂事件，这里不再需要旧的分流模式单选 */

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

/* ---------------- 账号与安全（改用户名 / 密码） ---------------- */
const Account = (function () {
  const SRC = {
    file: ["已启用 · 界面设置", "ok"],
    env: ["已启用 · 环境变量", "ok"],
    default: ["初始密码 admin · 建议立即修改", "warn"],
    open: ["未启用 · 任何人可访问", "bad"],
  };
  let info = { required: false, user: "", source: "open", min_len: 8 };

  function render() {
    const box = $("#acctStatus");
    if (!box) return;
    clear(box);
    const s = SRC[info.source] || SRC.open;
    const row = el("div", "row wrap");
    row.append(
      el("span", "lbl", "当前状态"),
      pill(s[0], s[1]),
      el("span", "muted", info.required ? `当前用户 ${info.user}` : "未设密码，打开即用"),
    );
    box.append(row);

    const oldLbl = $("#acctOldLbl"), oldInput = $("#acctOld");
    if (oldLbl) oldLbl.style.display = info.required ? "" : "none";
    if (oldInput) {
      oldInput.style.display = info.required ? "" : "none";
      oldInput.value = "";
    }
    const u = $("#acctUser");
    if (u && !u.value) u.value = info.user || "admin";
  }

  // 去掉容易混淆的 0/O/1/l/I，16 位约 95 bit 熵
  function gen() {
    const AB = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
    const bytes = new Uint8Array(16);
    if (window.crypto && crypto.getRandomValues) {
      crypto.getRandomValues(bytes);
    } else {
      for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    }
    let out = "";
    for (const b of bytes) out += AB[b % AB.length];
    $("#acctNew").value = out;
    $("#acctNew2").value = out;
    toast(`已生成随机密码：${out}`);
  }

  async function save() {
    const user = ($("#acctUser").value || "").trim() || "admin";
    const old = $("#acctOld").value;
    const p = $("#acctNew").value;
    const p2 = $("#acctNew2").value;
    const min = info.min_len || 8;

    if (!p) { toast("请填写新密码"); $("#acctNew").focus(); return; }
    if (p.length < min) { toast(`新密码至少 ${min} 位`); $("#acctNew").focus(); return; }
    if (p !== p2) { toast("两次输入的密码不一致"); $("#acctNew2").focus(); return; }
    if (info.required && !old) { toast("请先填写原密码"); $("#acctOld").focus(); return; }

    const d = await api("/api/auth/password", "POST", { old, user, password: p });
    setSession(d.token);
    const who = $("#who");
    if (who) who.textContent = d.user || user;
    $("#acctOld").value = ""; $("#acctNew").value = ""; $("#acctNew2").value = "";
    toast("已保存，其它设备需重新登录");
    await boot();
  }

  async function boot() {
    try {
      info = await (await fetch("/api/auth/info")).json();
    } catch (e) { /* 保持默认，不阻塞页面 */ }
    render();
  }

  $("#btnAcctSave").addEventListener("click", () => safe(save, "保存账号"));
  $("#btnAcctGen").addEventListener("click", () => gen());
  $("#acctNew2").addEventListener("keydown", (e) => { if (e.key === "Enter") safe(save, "保存账号"); });

  // 每次切到「账号」页都重新读一次状态（登录前后状态不同）
  const navBtn = document.querySelector('.nav button[data-tab="account"]');
  if (navBtn) navBtn.addEventListener("click", () => { boot(); });

  return { boot };
})();

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
  autoTcpProbe();
  loadBackups().catch(() => {});
  Traffic.start();
  if (typeof LogPage !== "undefined") LogPage.boot();
  if (typeof Account !== "undefined") Account.boot();
}

/* ---------------- 首页仪表板 ----------------
   把「能不能用」的关键信息摆到一屏：出口活着没、现在是哪个节点、走什么分流、
   哪个节点最快、订阅还新不新。所有控件都直接对接真实接口，不做假动作。 */
const GEAR_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
  + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
  + '<circle cx="12" cy="12" r="3"/>'
  + '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06'
  + 'a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09'
  + 'A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83'
  + 'l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09'
  + 'A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0'
  + 'l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09'
  + 'a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83'
  + 'l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09'
  + 'a1.65 1.65 0 0 0-1.51 1z"/></svg>';

/* ---------------- 代理分组（仪表板「代理组」卡片用） ----------------
   后端 sb_groups = [{name, nodes:[节点名], node:选中的节点}]，只有「当前分组」
   参与出口。node 为空 = 自动（延迟最低），此时 sing-box 用 urltest 自己挑。 */
function groupList() { return (STATE?.config?.outlet?.sb_groups) || []; }

function activeGroup() {
  const gs = groupList();
  const cur = STATE?.config?.outlet?.sb_group;
  return gs.find((g) => g.name === cur) || gs[0] || { name: "PROXY", nodes: [], node: "" };
}

/* 现在真正可用的节点（有出站、没被停用） */
function aliveNodes() {
  const o = STATE?.config?.outlet || {};
  const dis = new Set(o.sb_disabled || []);
  return (o.sb_nodes || []).filter((n) => n.outbound && !dis.has(n.name));
}

/* 当前分组里的节点，顺序沿用 sb_nodes（跟着测速排序走） */
function groupNodes() {
  const names = new Set(activeGroup().nodes || []);
  return aliveNodes().filter((n) => names.has(n.name));
}

/* 分组里延迟最低的那个（已测过的才算） */
function groupBest() {
  let best = null;
  groupNodes().forEach((n) => {
    const r = delayMap[n.name];
    if (!r || !r.reachable || r.ms === null || r.ms === undefined) return;
    if (!best || r.ms < best.r.ms) best = { n, r };
  });
  return best;
}

const Dash = (function () {
  let last = null;   // 最近一次 /api/outlet/stats

  const MODE_TEXT = {
    none: "未启用出口", manual: "手动代理地址", warp: "Cloudflare WARP", singbox: "sing-box",
  };

  function nodesAlive() {
    return aliveNodes();
  }

  function gotoTab(name, focus) {
    switchTab(name);
    if (!focus) return;
    setTimeout(() => {
      const box = focus === "route" ? $("#cardRoute") : null;
      if (box) box.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 80);
  }

  /* ---- 核心状态 ---- */
  function renderCore() {
    const o = STATE?.config?.outlet || {};
    const mode = o.mode || "none";
    const running = !!(last && last.running);
    const dot = $("#coreDot"), main = $("#coreState"), sub = $("#coreSub");
    const btn = $("#btnCoreToggle");
    if (!dot || !main) return;

    dot.className = "ddot";
    if (mode === "none" || mode === "manual") {
      dot.classList.add("warn");
      main.textContent = "未启用出口";
      sub.textContent = mode === "manual"
        ? `手动代理：${o.http || "未填写地址"}`
        : "只下发镜像源，流量不走代理";
      if (btn) btn.hidden = true;
      return;
    }

    const g = activeGroup();
    const detail = mode === "singbox"
      ? `sing-box · 端口 ${o.sb_port || 7890} · ${g.name}（${(g.nodes || []).length} 个节点）`
      : `WARP · 端口 ${o.warp_port || 9091}`;
    if (running) {
      dot.classList.add("ok");
      main.textContent = "正在运行";
      sub.textContent = detail;
    } else {
      dot.classList.add("bad");
      main.textContent = "已停止";
      sub.textContent = detail;
    }
    if (btn) {
      btn.hidden = false;
      btn.textContent = running ? "停止" : "启动";
      btn.classList.toggle("primary", !running);
    }
  }

  async function refreshStats() {
    try {
      const d = await api("/api/outlet/stats");
      onStats(d);
    } catch (e) { /* 401 等由 api 统一处理 */ }
  }

  async function toggleCore() {
    const o = STATE?.config?.outlet || {};
    const running = !!(last && last.running);
    const act = running ? "stop" : "start";
    if (o.mode === "none" || o.mode === "manual") { toast("先在下面选一个出口方式"); return; }
    // 分组没固定节点也没关系（自动 = urltest 挑最快的），但整组为空就起不来
    if (o.mode === "singbox" && !running && !groupNodes().length) {
      toast("当前分组没有可用节点，去「订阅」页添加订阅或调整分组");
      return;
    }
    if (o.mode === "singbox") await sb(act);
    else await warp(act);
    toast(`${o.mode === "singbox" ? "sing-box" : "WARP"} ${running ? "已停止" : "启动中…"}`);
    await refreshStats();
    // 容器起停要一两秒，稍后再对一次状态，避免按钮文字比实际慢半拍
    setTimeout(refreshStats, 1800);
  }

  /* ---- 代理组：分组胶囊 + 节点胶囊（照 v2rayA 的做法：选组 → 选节点） ---- */
  let liveNow = "";   // 「自动」时 sing-box 实际在用的节点（由 /api/outlet/stats 带回）

  /* 现在真正在用的节点：固定就是固定的；自动优先用 sing-box 报的 now，
     容器没起时退到本组测速最快的那个，只为让人看到「自动」指的是哪台。 */
  function currentNode(g) {
    if (g.node) return g.node;
    if (liveNow && (g.nodes || []).includes(liveNow)) return liveNow;
    const best = groupBest();
    return best ? best.n.name : "";
  }

  function renderGroup() {
    const g = activeGroup();
    const pinned = !!g.node;
    const cur = currentNode(g);

    const gn = $("#dashGroupName");
    if (gn) gn.textContent = g.name;
    const gc = $("#dashGroupCount");
    if (gc) gc.textContent = `${(g.nodes || []).length} 个节点`;
    const lg = $("#dashLatGroup");
    if (lg) lg.textContent = g.name;

    const nn = $("#dashNodeName");
    if (nn) nn.textContent = cur || "自动（延迟最低）";
    const btn = $("#btnDashNode");
    if (btn) {
      btn.title = pinned
        ? `当前固定「${g.node}」，点这里换节点或改回自动`
        : (cur
          ? `自动选择，当前走「${cur}」（sing-box 每 ${g.interval || 60}s 重测一次），点这里手动指定`
          : "自动挑本组延迟最低的节点，点这里手动指定");
    }

    const node = cur ? groupNodes().find((n) => n.name === cur) : null;
    const meta = $("#dashNodeMeta");
    if (meta) {
      meta.textContent = node
        ? (node.type || "节点")
        : `${(g.nodes || []).length} 个节点`;
      meta.title = node ? `${node.server}:${node.port}` : "";
    }

    // 「自动（N 个成员）」/「已固定」：一眼看出这个分组现在是自动还是钉死的
    const mode = $("#dashNodeMode");
    if (mode) {
      const n = (g.nodes || []).length;
      mode.textContent = pinned ? "已固定"
        : (g.strategy === "select" ? `手动（${n} 个成员）` : `自动（${n} 个成员）`);
      mode.className = "dpill" + (pinned ? " pin" : "");
      mode.title = pinned
        ? "已固定到这个节点，sing-box 不会再自动挑"
        : (g.strategy === "select"
          ? "分组类型是「手动选择」：用组内第一台，不自动挑；齿轮里可改成最小时延优先"
          : "sing-box 用 urltest 自动挑延迟最低的，齿轮里可改探测地址与间隔");
    }

    // 两种延迟并排显示：TCP 握手 / 实测网址（同一台节点两个数常常差很远）
    const pd = $("#dashDelay");
    if (pd) {
      const r = cur ? latOf(cur, "tcp") : null;
      pd.textContent = latShort(r, "tcp");
      pd.className = "dpill" + (r ? ` ${delayKind(r)}` : "");
      pd.title = r ? delayTitle(r) : "点「节点延迟」卡片右侧的「测速」跑一轮 TCP 握手";
    }
    const ph = $("#dashDelayHttp");
    if (ph) {
      const r = cur ? latOf(cur, "http") : null;
      ph.textContent = latShort(r, "http");
      ph.className = "dpill" + (r ? ` ${delayKind(r)}` : "");
      ph.title = r
        ? delayTitle(r)
        : "还没实测过：在「订阅」页的「目标 ▾」里选一个网址，再点「测速」";
    }
  }

  /* 分组菜单：✓ 当前分组 / 齿轮配测速 / 垃圾桶删除 / + 创建 */
  function groupMenu() {
    const cur = activeGroup().name;
    const items = [{ node: el("div", "pop-head", "代理分组") }];
    groupList().forEach((g) => {
      const on = g.name === cur;
      const row = el("div", "pg-row" + (on ? " on" : ""));
      row.appendChild(el("span", "pg-check", on ? "✓" : ""));
      row.appendChild(el("span", "pg-name", g.name));

      const gear = el("button", "pg-ico");
      gear.type = "button";
      gear.title = `测速配置「${g.name}」：探测 URL / 间隔 / 类型`;
      gear.innerHTML = GEAR_SVG;
      gear.addEventListener("click", (e) => {
        e.stopPropagation();
        Pop.close();
        safe(() => openProbeEditor(g), "测速配置");
      });
      row.appendChild(gear);

      if (groupList().length > 1) {
        const del = el("button", "pg-ico del");
        del.type = "button";
        del.title = `删除分组「${g.name}」`;
        del.innerHTML = TRASH_SVG;
        del.addEventListener("click", (e) => {
          e.stopPropagation();
          Pop.close();
          safe(() => delGroup(g), "删除分组");
        });
        row.appendChild(del);
      }

      row.addEventListener("click", () => {
        Pop.close();
        if (!on) safe(() => switchGroup(g.name), "切换分组");
      });
      items.push({ node: row });
    });
    items.push({ sep: true });
    const add = el("div", "pop-new");
    add.appendChild(el("span", "pg-check", "＋"));
    add.appendChild(el("span", "pg-name", "创建"));
    add.addEventListener("click", () => {
      Pop.close();
      safe(() => openGroupEditor(null), "新建分组");
    });
    items.push({ node: add });
    return items;
  }

  /* 齿轮：分组自己的测速参数（探测 URL / 间隔 / 类型）。
     这些值直接写进 sing-box 的 urltest 出站，不是界面装饰。 */
  function openProbeEditor(g) {
    const mask = el("div", "modal-mask");
    const box = el("div", "modal geditor");

    const h = el("h3", "ged-title");
    h.appendChild(document.createTextNode("代理分组 "));
    h.appendChild(el("span", "ged-gname", g.name));
    box.appendChild(h);
    box.appendChild(el("p", "modal-note",
      "「自动」由 sing-box 的 urltest 出站实现：按下面的间隔访问探测地址，"
      + "在本组里挑延迟最低的节点。改完保存后，重启 sing-box 生效。"));

    box.appendChild(el("label", "modal-lbl", "探测 URL"));
    const url = el("input", "modal-input");
    url.value = g.url || "https://www.gstatic.com/generate_204";
    url.placeholder = "https://www.gstatic.com/generate_204";
    box.appendChild(url);

    box.appendChild(el("label", "modal-lbl", "探测间隔"));
    const iv = el("input", "modal-input");
    iv.value = `${g.interval || 60}s`;
    iv.placeholder = "60s";
    box.appendChild(iv);
    box.appendChild(el("p", "modal-hint", "填秒数，10 ~ 3600 之间；写 60 或 60s 都行。"));

    box.appendChild(el("label", "modal-lbl", "类型"));
    const sel = el("select", "modal-input");
    [["urltest", "最小时延优先"], ["select", "手动选择"]].forEach(([v, t]) => {
      const op = el("option", "", t);
      op.value = v;
      sel.appendChild(op);
    });
    sel.value = g.strategy === "select" ? "select" : "urltest";
    box.appendChild(sel);

    const row = el("div", "modal-btns");
    const cancel = el("button", "", "取消");
    const ok = el("button", "primary", "保存");
    cancel.addEventListener("click", () => mask.remove());
    ok.addEventListener("click", () => safe(async () => {
      const interval = String(iv.value || "").trim().replace(/s$/i, "");
      ok.disabled = true;
      try {
        const d = await api("/api/groups", "POST", {
          config: { name: g.name, url: url.value.trim(), interval: interval || g.interval,
                    strategy: sel.value },
        });
        if (d.ok === false) { toast(d.message || "保存失败"); return; }
        mask.remove();
        toast(d.message + (d.need_restart ? "，重启 sing-box 后生效" : ""));
        await loadState();
      } finally { ok.disabled = false; }
    }, "保存测速配置"));
    row.append(cancel, ok);
    box.appendChild(row);

    mask.appendChild(box);
    mask.addEventListener("click", (e) => { if (e.target === mask) mask.remove(); });
    mask.addEventListener("keydown", (e) => {
      if (e.key === "Escape") mask.remove();
      if (e.key === "Enter") ok.click();
    });
    document.body.appendChild(mask);
    url.focus();
    url.select();
  }

  /* 节点菜单：自动（延迟最低）在最前，下面是本组节点（协议 + 延迟） */
  function nodeMenu() {
    const g = activeGroup();
    const items = [{ node: el("div", "pop-head", `${g.name} · ${(g.nodes || []).length} 个节点`) }];

    const auto = el("div", "nd-row");
    auto.appendChild(el("span", "nd-check", g.node ? "" : "✓"));
    const am = el("div", "nd-main");
    am.appendChild(el("div", "nd-name", "自动（延迟最低）"));
    const best = groupBest();
    // 「自动」到底用哪台：sing-box 跑着就以它报的为准，没跑就退到本组最快
    const pick = liveNow && (g.nodes || []).includes(liveNow)
      ? liveNow : (best ? best.n.name : "");
    const pickTcp = pick ? latOf(pick, "tcp") : null;
    const pickHttp = pick ? latOf(pick, "http") : null;
    const bestBits = pick
      ? [`${liveNow ? "当前走" : "本组最快"} ${pick}`]
      : [];
    if (pickTcp) bestBits.push(`TCP ${msShort(pickTcp)}`);
    if (pickHttp) bestBits.push(`HTTP ${msShort(pickHttp)}`);
    am.appendChild(el("div", "nd-meta", bestBits.length
      ? bestBits.join(" · ")
      : (g.strategy === "select" ? "类型是手动选择，不会自动挑" : "按延迟自动挑本组最快的节点")));
    auto.appendChild(am);
    auto.addEventListener("click", () => {
      Pop.close();
      if (g.strategy === "select") {
        toast("这个分组是「手动选择」类型，先在齿轮里改成「最小时延优先」");
        return;
      }
      safe(() => selectNode(""), "自动选择");
    });
    items.push({ node: auto });

    const list = groupNodes();
    if (list.length) items.push({ sep: true });
    list.forEach((n) => {
      const tcp = latOf(n.name, "tcp");
      const http = latOf(n.name, "http");
      const row = el("div", "nd-row");
      row.appendChild(el("span", "nd-check", n.name === g.node ? "✓" : ""));
      const main = el("div", "nd-main");
      const nameRow = el("div", "nd-name");
      nameRow.appendChild(document.createTextNode(n.name));
      // 自动模式下把 sing-box 正在用的那台标出来，省得去猜「自动」选了谁
      if (!g.node && n.name === liveNow) nameRow.appendChild(el("span", "nd-live", "当前"));
      main.appendChild(nameRow);
      // 两种延迟一起给：TCP 是握手，HTTP 是真出网，差一个量级很正常
      const bits = [n.type || ""];
      if (tcp) bits.push(`TCP ${msShort(tcp)}`);
      if (http) bits.push(`HTTP ${msShort(http)}`);
      const meta = el("div", "nd-meta", bits.filter(Boolean).join(" · "));
      meta.title = [tcp && delayTitle(tcp), http && delayTitle(http)].filter(Boolean).join("\n");
      main.appendChild(meta);
      row.appendChild(main);
      row.addEventListener("click", () => {
        Pop.close();
        safe(() => selectNode(n.name), "切换节点");
      });
      items.push({ node: row });
    });
    if (!list.length) {
      items.push({ node: el("div", "nd-meta", "这个分组还没有节点，点下面「编辑分组」勾进来。") });
    }
    return items;
  }

  /* 本机能不能做 URL 实测：被后端降级过一次就别再反复试（换页面就重算） */
  let httpBlocked = false;

  /* 分组编辑弹窗：改名 + 勾选成员（支持全选 / 反选 / 清空 / 筛选，不用一个个点） */
  function openGroupEditor(g) {
    const isNew = !g;
    const mask = el("div", "modal-mask");
    const box = el("div", "modal geditor");
    box.appendChild(el("h3", "", isNew ? "新建代理分组" : `编辑分组「${g.name}」`));
    box.appendChild(el("p", "modal-note", isNew
      ? "分组只是把节点分堆，出口只用「当前分组」的节点。默认全勾上，按需取消。"
      : "取消勾选即把节点移出本组（节点本身不会被删除）；改名字后点保存即生效。"));

    box.appendChild(el("label", "modal-lbl", "分组名称"));
    const inp = el("input", "modal-input");
    inp.value = isNew ? "" : g.name;
    inp.placeholder = "例如 CLOUDFLARE / 香港 / 备用";
    inp.maxLength = 20;
    box.appendChild(inp);

    box.appendChild(el("label", "modal-lbl", "包含的节点"));
    const all = nodesAlive();
    const chosen = new Set(isNew ? all.map((n) => n.name) : (g.nodes || []));

    // 工具栏：筛选 + 已选计数 + 两种测速 + 批量操作（都只作用于当前筛选结果）
    const bar = el("div", "ged-bar");
    const filter = el("input", "ged-filter");
    filter.type = "search";
    filter.placeholder = "筛选节点（名字 / 协议）";
    const cnt = el("span", "ged-count", "");

    // TCP / URL 两个测速入口：点一下就地测这批节点，结果直接落到下面两列
    const chipTcp = el("button", "ged-mode", "TCP");
    const chipUrl = el("button", "ged-mode", "URL");
    chipTcp.type = chipUrl.type = "button";
    chipTcp.title = "TCP 握手（左列数字）：只连节点端口，快，但只证明端口通。"
      + "打开弹窗会自动测好；点一下＝重测列表里的全部节点（勾没勾都测）";
    chipUrl.title = `URL 实测（右列数字）：经节点访问 ${shortUrl(probeUrl())} 的真实往返耗时。`
      + "打开弹窗会自动测好；点一下＝重测列表里的全部节点（勾没勾都测）";

    const bAll = el("button", "linkbtn", "全选");
    const bInv = el("button", "linkbtn", "反选");
    const bNone = el("button", "linkbtn", "清空");
    bAll.title = "勾上当前筛选结果里的全部节点";
    bInv.title = "筛选结果里已勾的取消、没勾的勾上";
    bNone.title = "把筛选结果里的节点全部移出本组";
    const bOnly = el("button", "linkbtn", "仅留");
    bOnly.title = "只保留筛选结果里的节点，其余移出本组（先在左边筛一下）";
    bAll.type = bInv.type = bNone.type = bOnly.type = "button";
    bar.append(filter, cnt, chipTcp, chipUrl, bAll, bInv, bNone, bOnly);
    box.appendChild(bar);

    /* 这个分组「URL 实测」到底访问哪里：优先用齿轮里配的探测地址
       （sing-box 的 urltest 用的就是它），没配就用页面当前的测速目标。 */
    function probeUrl() {
      const u = ((g && g.url) || "").trim();
      if (/^https?:\/\//i.test(u)) return u;
      return testTarget().url || TEST_TARGETS[0].url;
    }

    /* 一格延迟：TCP 握手 / URL 实测各占一列，测过才给数字，没测就是「—」 */
    function paintCell(node, name, mode) {
      const r = latOf(name, mode);
      node.classList.toggle("none", !r);
      node.classList.toggle("ok", !!r && delayKind(r) === "ok");
      node.classList.toggle("warn", !!r && delayKind(r) === "warn");
      node.classList.toggle("bad", !!r && delayKind(r) === "bad");
      node.textContent = r ? msShort(r) : "—";
      node.title = r ? delayTitle(r)
        : `${mode === "http" ? "URL 实测" : "TCP 握手"}：还没测过，点这一格就单独测它`;
    }

    function paintRow(row) {
      paintCell(row.tcpEl, row.name, "tcp");
      paintCell(row.urlEl, row.name, "http");
    }

    const list = el("div", "ged-list");
    const rows = [];
    all.forEach((n) => {
      const lab = el("label", "ged-item");
      const cb = el("input");
      cb.type = "checkbox";
      cb.checked = chosen.has(n.name);
      cb.addEventListener("change", () => {
        if (cb.checked) chosen.add(n.name); else chosen.delete(n.name);
        syncCount();
      });
      lab.appendChild(cb);
      lab.appendChild(el("span", "ged-name", n.name));
      const tcpEl = el("span", "ged-ms tcp");
      const urlEl = el("span", "ged-ms url");
      lab.append(tcpEl, urlEl);
      lab.appendChild(el("span", "ged-type", n.type || ""));
      lab.setAttribute("data-q", `${n.name} ${n.type || ""}`.toLowerCase());
      const row = { lab, cb, name: n.name, tcpEl, urlEl };
      // 点某一格＝只测这一台的这一种，免得为了一个数测全部
      [["tcp", tcpEl], ["http", urlEl]].forEach(([mode, cell]) => {
        cell.addEventListener("click", (e) => {
          e.preventDefault();     // label 里的点击会顺带勾选框，拦掉
          e.stopPropagation();
          if (busy) return;
          testHere(mode, row);
        });
      });
      paintRow(row);
      rows.push(row);
      list.appendChild(lab);
    });
    if (!all.length) list.appendChild(el("div", "muted", "还没有可用节点，先去「订阅」页添加订阅。"));
    box.appendChild(list);

    const visibleRows = () => rows.filter((r) => !r.lab.hidden);
    // 没筛选就测全部；筛了就只测筛出来的（先筛后测，省时间）
    const targetRows = () => (filter.value.trim() ? visibleRows() : rows);

    let busy = false;       // 手动测速中
    let autoProbe = null;   // 打开弹窗时的补测（Promise，手动测速排在它后面）

    /* 焦点是哪一种测速：只用来标一下刚才测的是哪列（两列数字都照常显示） */
    function setMode(mode) {
      chipTcp.classList.toggle("on", mode === "tcp");
      chipUrl.classList.toggle("on", mode === "http");
    }

    function setBusy(mode, n) {
      busy = !!mode;
      chipTcp.disabled = chipUrl.disabled = busy;
      chipTcp.classList.toggle("busy", mode === "tcp");
      chipUrl.classList.toggle("busy", mode === "http");
      chipTcp.textContent = mode === "tcp" ? "TCP…" : "TCP";
      chipUrl.textContent = mode === "http" ? "URL…" : "URL";
      [bAll, bInv, bNone, bOnly].forEach((b) => { if (busy) b.disabled = true; });
      if (busy) cnt.textContent = `测速中 ${n} 个…`;
    }

    /* 就地测速：结果写回全局缓存（节点页 / 首页用的是同一份），
       这两列立刻刷新，列表按这次的延迟重排，超时的沉底 */
    async function testHere(mode, only) {
      if (busy) return;
      const names = only ? [only.name] : targetRows().map((r) => r.name);
      if (!names.length) { toast("当前筛选下没有节点可测"); return; }
      setBusy(mode, names.length);
      try {
        // 补测还在跑就先等它，免得两批结果前后脚回来互相覆盖
        if (autoProbe) await autoProbe.catch(() => {});
        await runNodeTest(names, mode, mode === "http" ? probeUrl() : null, { sort: !only });
        rows.forEach(paintRow);
        // 没 Docker 时 URL 实测会被后端降级成 TCP，结果落在 TCP 列，高亮别搞错
        const used = mode === "http" && !names.some((n) => latOf(n, "http")) ? "tcp" : mode;
        setMode(used);
        if (!only) sortRows(used);
      } catch (e) {
        toast(`测速失败：${e.message || e}`);
      } finally {
        setBusy(null);
        syncCount();
      }
    }

    // 名次：有耗时 → 通但没耗时 → 超时/失败 → 没测过，同档按毫秒升序
    function sortRows(mode) {
      const key = (r) => latOf(r.name, mode);
      const idx = new Map(rows.map((r, i) => [r.name, i]));
      rows.sort((a, b) => {
        const ra = key(a), rb = key(b);
        const ka = delayRank(ra), kb = delayRank(rb);
        if (ka !== kb) return ka - kb;
        if (ka === 0 && ra.ms !== rb.ms) return ra.ms - rb.ms;
        return idx.get(a.name) - idx.get(b.name);
      });
      rows.forEach((r) => list.appendChild(r.lab));
    }

    chipTcp.addEventListener("click", () => testHere("tcp"));
    chipUrl.addEventListener("click", () => testHere("http"));
    // 进来先按已有的结果决定高亮哪一列（有 TCP 结果就高亮 TCP）
    setMode(rows.some((r) => latOf(r.name, "tcp")) || !rows.some((r) => latOf(r.name, "http"))
      ? "tcp" : "http");

    /* 打开弹窗就把两列都填上：进页面只测过「当前分组」的节点，
       列表里没勾的（不在本组）还是空的，而且 URL 实测以前还得单独点一次。
       这里按缺什么补什么：先握手（快，数字先出来），再实测 URL；都不写日志、不动顺序。
       没有 Docker 的环境 URL 会被后端降级成 TCP，那种情况记下来，本次会话不再重试。 */
    const missTcp = rows.filter((r) => !latOf(r.name, "tcp")).map((r) => r.name);
    const missUrl = httpBlocked ? []
      : rows.filter((r) => !latOf(r.name, "http")).map((r) => r.name);
    if (missTcp.length || missUrl.length) {
      autoProbe = (async () => {
        if (missTcp.length) {
          cnt.textContent = `补测 TCP ${missTcp.length} 个…`;
          await runNodeTest(missTcp, "tcp", null, { silent: true, quiet: true });
          rows.forEach(paintRow);
        }
        if (missUrl.length) {
          cnt.textContent = `补测 URL ${missUrl.length} 个…`;
          const d = await runNodeTest(missUrl, "http", probeUrl(), { silent: true, quiet: true });
          rows.forEach(paintRow);
          // 被降级成 TCP 且原因是没 Docker → 本次会话别再反复试；
          // 只是「上一次实测还在跑」这种偶发冲突就不记，下次开弹窗再补
          const note = (d && d.note) || "";
          if (d && d.mode !== "http" && /docker/i.test(note)) httpBlocked = true;
        }
      })().catch(() => { /* 补测失败就保持原样，不影响选成员 */ })
        .finally(() => { autoProbe = null; syncCount(); });
    }

    function syncCount() {
      // 测速/补测过程中计数栏在报进度，别被勾选事件抢回去
      if (busy || autoProbe) return;
      cnt.textContent = all.length ? `已选 ${chosen.size} / ${all.length}` : "";
      const any = visibleRows().length > 0;
      [bAll, bInv, bNone].forEach((b) => { b.disabled = !any; });
      bOnly.disabled = !any || !filter.value.trim();
    }

    function applyFilter() {
      const q = filter.value.trim().toLowerCase();
      rows.forEach((r) => {
        r.lab.hidden = !!q && !(r.lab.getAttribute("data-q") || "").includes(q);
      });
      syncCount();
    }
    filter.addEventListener("input", applyFilter);

    bAll.addEventListener("click", () => {
      visibleRows().forEach((r) => { r.cb.checked = true; chosen.add(r.name); });
      syncCount();
    });
    bInv.addEventListener("click", () => {
      visibleRows().forEach((r) => {
        r.cb.checked = !r.cb.checked;
        if (r.cb.checked) chosen.add(r.name); else chosen.delete(r.name);
      });
      syncCount();
    });
    bNone.addEventListener("click", () => {
      visibleRows().forEach((r) => { r.cb.checked = false; chosen.delete(r.name); });
      syncCount();
    });

    // 「仅留」：先筛出要保留的，再一键把其余全部移出，省得一个个取消
    bOnly.addEventListener("click", () => {
      const keep = new Set(visibleRows().map((r) => r.name));
      chosen.clear();
      rows.forEach((r) => {
        const on = keep.has(r.name);
        r.cb.checked = on;
        if (on) chosen.add(r.name);
      });
      syncCount();
    });

    const row = el("div", "modal-btns");
    const cancel = el("button", "", "取消");
    const del = el("button", "danger", "删除分组");
    const ok = el("button", "primary", isNew ? "创建" : "保存");
    del.hidden = isNew || groupList().length <= 1;
    cancel.addEventListener("click", () => mask.remove());
    del.addEventListener("click", () => {
      mask.remove();
      safe(() => delGroup(g), "删除分组");
    });
    ok.addEventListener("click", () => safe(async () => {
      const name = inp.value.trim();
      if (!name) { toast("先给分组起个名字"); inp.focus(); return; }
      const nodes = Array.from(chosen);
      ok.disabled = true;
      try {
        if (isNew) {
          const d = await api("/api/groups", "POST", { create: { name, nodes } });
          if (d.ok === false) { toast(d.message || "创建失败"); return; }
          mask.remove();
          toast(d.message + (d.need_restart ? "，重启 sing-box 后生效" : ""));
        } else {
          if (name !== g.name) {
            const r = await api("/api/groups", "POST", { rename: { from: g.name, to: name } });
            if (r.ok === false) { toast(r.message || "改名失败"); return; }
          }
          const keep = g.node && nodes.includes(g.node) ? g.node : "";
          const d = await api("/api/groups", "POST", { update: { name, nodes, node: keep } });
          if (d.ok === false) { toast(d.message || "保存失败"); return; }
          mask.remove();
          toast(d.message + (d.need_restart ? "，重启 sing-box 后生效" : ""));
        }
        await loadState();
        probeGroup();
      } finally { ok.disabled = false; }
    }, "保存分组"));
    row.append(cancel, del, ok);
    box.appendChild(row);

    mask.appendChild(box);
    mask.addEventListener("click", (e) => { if (e.target === mask) mask.remove(); });
    mask.addEventListener("keydown", (e) => {
      if (e.key === "Escape") mask.remove();
      if (e.key === "Enter" && e.target !== filter) ok.click();
    });
    document.body.appendChild(mask);
    if (isNew) inp.focus(); else inp.select();
    syncCount();
  }

  async function delGroup(g) {
    if (!confirm(`删除分组「${g.name}」？组内节点不会被删除。`)) return;
    const d = await api("/api/groups", "POST", { delete: g.name });
    if (d.ok === false) { toast(d.message || "删除失败"); return; }
    toast(d.message + (d.need_restart ? "，重启 sing-box 后生效" : ""));
    await loadState();
    probeGroup();
  }

  async function selectNode(name) {
    const g = activeGroup().name;
    const d = await api("/api/nodes", "POST", { node: name });
    if (d.ok === false) { toast(d.message || "切换失败"); return; }
    toast(name ? `「${g}」固定到「${name}」` : `「${g}」改回自动（延迟最低）`);
    await loadState();
  }

  async function switchGroup(name) {
    const d = await api("/api/groups", "POST", { active: name });
    if (d.ok === false) { toast(d.message || "切换失败"); return; }
    toast(`已切到「${name}」` + (d.need_restart ? "，重启 sing-box 后生效" : ""));
    await loadState();
    probeGroup();
  }

  /* 给当前分组刷一轮 TCP 握手，进页面 / 换组 / 改成员后调用 */
  function probeGroup() {
    const names = groupNodes().map((n) => n.name);
    if (!names.length) return Promise.resolve();
    return runNodeTest(names, "tcp", null, { silent: true, sort: true }).catch(() => {});
  }

  /* ---- 节点延迟：测的是「当前代理组」里的节点，TCP / HTTP 两列并排 ---- */
  function renderLatency() {
    const box = $("#dashLatency");
    if (!box) return;
    clear(box);
    const g = activeGroup();
    const list = groupNodes();
    if (!list.length) {
      box.appendChild(el("div", "muted", "当前分组没有节点，用上面的「编辑分组」勾几个进来。"));
      return;
    }
    // 排序用 TCP（进页面自动跑的就是它）；没有 TCP 时退到任意一次结果
    const keyOf = (n) => latOf(n.name, "tcp") || delayMap[n.name] || null;
    const measured = list.filter((n) => {
      const r = keyOf(n);
      return r && r.reachable && r.ms !== null && r.ms !== undefined;
    });
    if (!measured.length) {
      box.appendChild(el("div", "muted",
        `点右上「测速」测这一组的 ${list.length} 个节点（按 TCP 握手）。`));
      return;
    }
    const rowsAll = list.map((n) => ({ n, r: keyOf(n) }))
      .sort((a, b) => {
        const ka = delayRank(a.r), kb = delayRank(b.r);
        if (ka !== kb) return ka - kb;
        if (ka === 0) return a.r.ms - b.r.ms;
        return 0;
      });
    let rows = rowsAll.slice(0, 5);
    // 当前出口（固定节点 / 自动选中的那台）延迟不一定最低，但它才是现在用的，得看得见
    const curName = g.node || (liveNow && (g.nodes || []).includes(liveNow) ? liveNow : "");
    if (curName && !rows.some((x) => x.n.name === curName)) {
      const hit = rowsAll.find((x) => x.n.name === curName);
      if (hit) rows = rows.slice(0, 4).concat([hit]);
    }
    const max = Math.max.apply(null, measured.map((n) => keyOf(n).ms)) || 1;
    rows.forEach(({ n, r }) => {
      const ok = r && r.reachable && r.ms !== null && r.ms !== undefined;
      const row = el("div", "dlrow" + (n.name === curName ? " cur" : ""));
      row.title = ok
        ? `点一下把「${g.name}」固定到「${n.name}」`
        : `「${n.name}」本次没测通，点一下仍可固定到它`;
      row.appendChild(el("div", "dlname", n.name));
      const bar = el("div", "dlbar");
      const fill = el("i", r ? delayKind(r) : "bad");
      fill.style.width = ok ? Math.max(8, Math.round((r.ms / max) * 100)) + "%" : "100%";
      if (!ok) fill.style.opacity = ".35";
      bar.appendChild(fill);
      row.appendChild(bar);
      row.appendChild(el("div", "dlms", ok ? `${r.ms}ms` : (r ? "TIMEOUT" : "—")));
      // 第二列：真经节点访问网址的耗时（没实测过就是 —）
      const h = latOf(n.name, "http");
      const hm = el("div", "dlms http", h ? msShort(h) : "—");
      hm.title = h ? delayTitle(h) : "还没实测过：在「订阅」页「目标 ▾」选个网址后点测速";
      row.appendChild(hm);
      row.addEventListener("click", () => safe(() => setActiveNode(n), "切换出口"));
      box.appendChild(row);
    });
  }

  /* ---- 订阅摘要 ---- */
  function renderSubs() {
    const box = $("#dashSubs");
    if (!box) return;
    const subs = STATE?.config?.outlet?.subscriptions || [];
    const cnt = $("#dashSubCount");
    if (cnt) cnt.textContent = String(subs.length);
    clear(box);
    if (!subs.length) {
      box.appendChild(el("div", "muted", "还没有订阅，去「订阅」页添加一个。"));
      return;
    }
    subs.slice(0, 4).forEach((s) => {
      const d = el("div", "dsub");
      d.dataset.key = subKey(s);
      const main = el("div", "dsub-main");
      main.appendChild(el("div", "dsub-name", s.name || s.url || "未命名订阅"));

      // 流量用量：机场在订阅响应头里给了才显示，没给就整行不出现
      if (hasUsage(s)) {
        const use = el("div", "dsub-use mono");
        use.appendChild(el("span", "", `已用 ${bytes2(s.usage.used)} / ${s.usage.total > 0 ? bytes2(s.usage.total) : "不限量"}`));
        const exp = fmtExpire(s.usage.expire);
        if (exp) use.appendChild(el("span", "", `到期 ${exp}`));
        main.appendChild(use);
        if (s.usage.total > 0) main.appendChild(usageBar(s.usage, "dsub-bar"));
      }

      const meta = el("div", "dsub-meta");
      meta.appendChild(el("span", "", `${s.count || 0} 节点`));
      meta.appendChild(el("span", "", s.updated_at ? `更新于 ${relTime(s.updated_at)}` : "还没更新过"));
      if (s.enabled === false) meta.appendChild(el("span", "", "已停用"));
      main.appendChild(meta);
      d.appendChild(main);

      // ⋮ 菜单：跟「订阅」页同一批动作，首页不用跳过去就能更新 / 改名 / 分享 / 删除
      d.appendChild(dotsBtn((btn) => Pop.toggle(btn, [
        { label: "更新", onClick: () => safe(() => updateSubs([subKey(s)]), "更新订阅") },
        { label: "修改", onClick: () => safe(() => editSub(s), "修改订阅") },
        { label: "分享", onClick: () => safe(() => shareSub(s), "分享订阅") },
        { sep: true },
        { label: "删除", danger: true, onClick: () => safe(() => delSubs([subKey(s)], subTitle(s)), "删除订阅") },
      ])));

      box.appendChild(d);
    });
  }

  /* ---- 分流模式（跟规则表的兜底动作是同一份数据） ---- */
  function renderFinal() {
    const M = Route.get();
    const fin = (M && M.final) || STATE?.config?.outlet?.sb_final || "direct";
    $$('input[name="dashFinal"]').forEach((r) => { r.checked = r.value === fin; });
  }

  async function setFinal(v) {
    try {
      await Route.setFinal(v);
      toast(v === "proxy" ? "已切到全局代理模式" : "已切到大陆白名单模式");
    } finally {
      renderFinal();
    }
  }

  /* ---- 出口方式 / TUN / 系统变量 ---- */
  async function setMode(v) {
    await save({ outlet: { mode: v } });
    // 出口页的单选/面板跟着切，别让两处显示不一致
    renderConfig(STATE.config);
    toast(`出口方式：${MODE_TEXT[v] || v}`);
    refreshStats();
  }

  async function setTun(on) {
    await save({ outlet: { sb_tun: !!on } });
    const sbTun = $("#sbTun");
    if (sbTun) sbTun.checked = !!on;
    toast(on ? "已开启 TUN（重启 sing-box 后生效）" : "已关闭 TUN");
  }

  async function setSys(on) {
    await save({ targets: { system: !!on } });
    const box = $("#t-system");     // 「下发」页的同一个勾选，别两处不一致
    if (box) box.checked = !!on;
    render();
    toast(on ? "已勾选系统环境变量，去「下发」页点应用才会写入" : "已取消系统环境变量");
  }

  function bind() {
    $$("[data-goto]").forEach((b) => {
      b.addEventListener("click", () => gotoTab(b.dataset.goto, b.dataset.focus));
    });

    const btnCore = $("#btnCoreToggle");
    if (btnCore) btnCore.addEventListener("click", () => safe(toggleCore, "出口启停"));
    const logs = $("#btnCoreLogs");
    if (logs) logs.addEventListener("click", () => gotoTab("logs"));
    const outlet = $("#btnCoreOutlet");
    if (outlet) outlet.addEventListener("click", () => gotoTab("outlet"));

    const gbtn = $("#btnDashGroup");
    if (gbtn) gbtn.addEventListener("click", () => Pop.toggle(gbtn, groupMenu(), { cls: "wide" }));

    const nbtn = $("#btnDashNode");
    if (nbtn) nbtn.addEventListener("click", () => Pop.toggle(nbtn, nodeMenu(), { cls: "wide" }));

    const ged = $("#btnDashGroupEdit");
    if (ged) ged.addEventListener("click", () => safe(() => openGroupEditor(activeGroup()), "编辑分组"));

    const mode = $("#dashMode");
    if (mode) mode.addEventListener("change", () => safe(() => setMode(mode.value), "切换出口方式"));

    const tun = $("#dashTun");
    if (tun) tun.addEventListener("change", () => safe(() => setTun(tun.checked), "切换 TUN"));

    const sys = $("#dashSys");
    if (sys) sys.addEventListener("change", () => safe(() => setSys(sys.checked), "切换系统变量"));

    $$('input[name="dashFinal"]').forEach((r) => {
      r.addEventListener("change", () => { if (r.checked) safe(() => setFinal(r.value), "切换分流模式"); });
    });

    const test = $("#btnDashTest");
    if (test) {
      // 按「目标 ▾」里存的对象测：TCP 握手或真实访问网址（两种延迟都要能看到）
      const paint = () => {
        const t = testTarget();
        test.textContent = t.tcp ? "测速 TCP" : "测速 HTTP";
        test.title = t.tcp
          ? "对本组节点做 TCP 握手测速（3 秒超时）"
          : `经每个节点真实访问 ${t.url}`;
      };
      paint();
      test.addEventListener("click", () => safe(async () => {
        const b = $("#btnDashTest");
        const names = groupNodes().map((n) => n.name);
        if (!names.length) { toast("当前分组没有节点"); return; }
        const t = testTarget();
        const mode = t.tcp ? "tcp" : "http";
        b.disabled = true;
        const label = b.textContent;
        b.textContent = "测速中…";
        try {
          await runNodeTest(names, mode, mode === "http" ? t.url : null, { sort: true });
        } finally { b.disabled = false; b.textContent = label; }
      }, "测速"));
    }

    const upd = $("#btnDashUpdateSubs");
    if (upd) upd.addEventListener("click", () => safe(async () => {
      const b = $("#btnDashUpdateSubs");
      b.disabled = true;
      b.textContent = "更新中…";
      try {
        const d = await api("/api/subs/update", "POST", {});
        toast(d.message || "订阅已更新");
        await loadState();
      } finally {
        b.disabled = false;
        b.textContent = "全部更新";
      }
    }, "更新订阅"));
  }

  function render() {
    const c = STATE?.config;
    if (!c) return;
    const o = c.outlet || {};
    const sel = $("#dashMode");
    if (sel && o.mode) sel.value = o.mode;
    const tun = $("#dashTun");
    if (tun) tun.checked = !!o.sb_tun;
    const sys = $("#dashSys");
    if (sys) sys.checked = !!c.targets?.system;
    renderCore();
    renderGroup();
    renderLatency();
    renderSubs();
    renderFinal();
  }

  function onStats(d) {
    last = d || null;
    renderCore();
    // 「自动」到底走了哪台：sing-box 的 urltest 自己挑，只有它知道
    const now = (d && d.now) || "";
    if (now !== liveNow) {
      liveNow = now;
      renderGroup();
    }
  }

  // 延迟/节点变化后由外部调（renderNodes / 测速结束时）
  function onNodes() {
    renderGroup();
    renderLatency();
  }

  bind();
  return { render, onStats, onNodes, refreshStats };
})();

/* ---------------- 实时流量图（仿 v2rayA） ---------------- */
const Traffic = (function () {
  const MAX = 60;
  let down = [], up = [];
  let canvas = null, ctx = null, timer = null;

  function resize() {
    if (!canvas) return;
    const w = canvas.clientWidth || 600;
    const h = canvas.clientHeight || 150;   // 高度交给 CSS（仪表板卡片比旧版矮）
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
    const w = canvas.clientWidth || 600, h = canvas.clientHeight || 150;
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.lineWidth = 1;
    for (let g = 1; g < 4; g++) {
      const y = h * g / 4;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }
    const all = down.concat(up);
    if (!all.some((v) => v > 0)) {
      // 空状态给一句话，别让整块区域看起来像加载失败
      ctx.fillStyle = "rgba(233,228,216,0.22)";
      ctx.font = '12px -apple-system, "Segoe UI", "PingFang SC", sans-serif';
      ctx.textAlign = "center";
      ctx.fillText("暂无流量 · 启动出口后这里会画出曲线", w / 2, h / 2);
      ctx.textAlign = "left";
      return;
    }
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
        Dash.onStats(d);
        return;
      }
      $("#tHint").textContent = d.mode === "warp" ? "出口：Cloudflare WARP" : "出口：sing-box";
      push(d);
      Dash.onStats(d);
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
