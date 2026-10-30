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

async function api(path, method = "GET", body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
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

/* ---------------- tabs ---------------- */
function switchTab(name) {
  const btn = $$(".tab").find((x) => x.dataset.tab === name);
  if (!btn) return;
  $$(".tab").forEach((x) => x.classList.remove("on"));
  $$("section").forEach((x) => x.classList.remove("on"));
  btn.classList.add("on");
  $(`#tab-${btn.dataset.tab}`).classList.add("on");
}

$$(".tab").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

// 深链：?tab=push 或 #push 直达指定页签
(function restoreTab() {
  const q = new URLSearchParams(location.search).get("tab");
  const h = decodeURIComponent(location.hash.replace(/^#/, ""));
  if (q) switchTab(q);
  else if (h) switchTab(h);
})();

/* ---------------- 渲染 ---------------- */
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
  t.appendChild(head);
  t.appendChild(el("tr")).appendChild(td);
  $("#env").textContent = `HOST_ROOT=${p.host_root}`;
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
$("#btnSubFetch").addEventListener("click", () => safe(async () => {
  const out = $("#subOut");
  clear(out);
  out.appendChild(el("div", "hint", "正在拉取订阅…"));
  const d = await api("/api/subs/fetch", "POST", {
    url: $("#inSubUrl").value.trim(),
    ua: $("#inSubUa").value.trim() || "clash.meta",
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
  (d.nodes || []).forEach((n) => {
    const o = el("option", "", `${n.name} · ${n.type}`);
    o.value = n.name;
    sel.appendChild(o);
  });
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

loadState().then(loadBackups).catch((e) => toast(`初始化失败：${e.message}`));
