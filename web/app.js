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
  $("#inSubUrl").value = c.outlet.sub_url || "";
  $("#inMirrors").value = (c.mirrors || []).join("\n");
  $("#inNoProxy").value = c.no_proxy || "";
  $("#t-dockerd").checked = !!c.targets.dockerd;
  $("#t-mirror").checked = !!c.targets.mirror;
  $("#t-ca").checked = !!c.targets.ca;
  $("#t-system").checked = !!c.targets.system;
  $("#allowRestart").checked = !!c.allow_restart_docker;
  $("#emhttpProxy").checked = !!c.emhttp_proxy;

  const sel = $("#selNode");
  clear(sel);
  (c.outlet.sb_nodes || []).forEach((n) => {
    const o = el("option", "", `${n.name} · ${n.type}`);
    o.value = n.name;
    sel.appendChild(o);
  });
  if (c.outlet.sb_node) sel.value = c.outlet.sb_node;
  renderNodeList(c.outlet.sb_nodes || []);
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

/* ---------------- 订阅 ---------------- */
function renderNodeList(nodes) {
  const box = $("#nodeList");
  clear(box);
  if (!(nodes || []).length) { box.appendChild(el("div", "hint", "暂无节点。拉取订阅后这里会列出可选出口。")); return; }
  const table = el("table", "data");
  const tr = el("tr");
  ["节点", "协议", "地址"].forEach((h) => tr.appendChild(el("th", "", h)));
  table.appendChild(tr);
  nodes.forEach((n) => {
    const row = el("tr");
    row.appendChild(el("td", "", n.name));
    row.appendChild(el("td")).appendChild(pill(n.type, "mute"));
    row.appendChild(el("td", "mono", `${n.server}:${n.port}`));
    table.appendChild(row);
  });
  box.appendChild(table);
  box.appendChild(el("div", "hint", "选好节点后到「出口」页选它并启动 sing-box。"));
}

$("#btnSubFetch").addEventListener("click", () => safe(async () => {
  const out = $("#subOut");
  clear(out);
  out.appendChild(el("div", "hint", "正在拉取订阅…"));
  const d = await api("/api/subs/fetch", "POST", {
    url: $("#inSubUrl").value.trim(),
    ua: $("#inSubUa").value.trim() || "clash.meta",
    proxy: $("#inSubProxy").value.trim(),
    raw: $("#inSubRaw").value,
  });
  clear(out);
  out.appendChild(pill("解析完成", "ok"));
  out.appendChild(el("div", "", d.message));
  if (d.unsupported && d.unsupported.length) {
    out.appendChild(el("div", "hint", `不支持的协议：${d.unsupported.join(", ")}（已跳过）`));
  }
  const sel = $("#selNode");
  clear(sel);
  sel.appendChild(el("option", "", "（未选择节点）"));
  (d.nodes || []).forEach((n) => {
    const o = el("option", "", `${n.name} · ${n.type}`);
    o.value = n.name;
    sel.appendChild(o);
  });
  renderNodeList(d.nodes || []);
}, "拉取订阅"));

$("#btnSubTest").addEventListener("click", () => safe(async () => {
  const out = $("#subOut");
  out.appendChild(el("div", "hint", "正在测速（TCP 连通耗时）…"));
  const d = await api("/api/subs/test", "POST", {});
  const table = el("table", "data");
  const tr = el("tr");
  ["节点", "TCP 耗时"].forEach((h) => tr.appendChild(el("th", "", h)));
  table.appendChild(tr);
  (d.results || []).forEach((r) => {
    const row = el("tr");
    row.appendChild(el("td", "", r.tag));
    row.appendChild(el("td")).appendChild(
      pill(r.reachable ? `${r.ms}ms` : "不可达", r.reachable ? (r.ms < 300 ? "ok" : "warn") : "bad"));
    table.appendChild(row);
  });
  out.appendChild(table);
  out.appendChild(el("div", "hint", "这是到节点服务器的 TCP 耗时，真实代理延迟略高于此值。"));
}, "测速"));

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

boot();
