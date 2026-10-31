/* 前端测速排序的回归测试：直接抽取 web/app.js 里的 delayRank / sortNodesByDelay 源码执行，
   避免「测试里另写一份算法」导致两边跑偏。
   用法：node tests/sort_check.js */
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");

function grab(name) {
  const start = src.indexOf("function " + name + "(");
  if (start < 0) throw new Error("app.js 里找不到函数 " + name);
  let i = src.indexOf("{", start), depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error("括号不匹配：" + name);
}

const factory = new Function([
  "var nodeCache = [];",
  "var delayMap = {};",
  "var STATE = null;",
  grab("delayRank"),
  grab("sortNodesByDelay"),
  "return {",
  "  delayRank: delayRank,",
  "  load: function (nodes, delays) { nodeCache = nodes; delayMap = delays; },",
  "  sort: function () { return sortNodesByDelay(); },",
  "  names: function () { return nodeCache.map(function (n) { return n.name; }); },",
  "};",
].join("\n"));

const FAILED = [];
function check(cond, msg) {
  console.log((cond ? "  PASS  " : "  FAIL  ") + msg);
  if (!cond) FAILED.push(msg);
}

const N = (...names) => names.map((n) => ({ name: n }));

console.log("\n[1] 按延迟升序，超时垫底");
let t = factory();
t.load(N("a", "b", "c", "d"), {
  a: { tag: "a", reachable: true, ms: 900 },
  b: { tag: "b", reachable: true, ms: 120 },
  c: { tag: "c", reachable: false, ms: null, mode: "tcp" },
  d: { tag: "d", reachable: true, ms: 300 },
});
check(t.sort() === true, "顺序发生变化，返回 true");
check(JSON.stringify(t.names()) === JSON.stringify(["b", "d", "a", "c"]),
  "排序结果：" + t.names().join(" > "));

console.log("\n[2] 没测过的节点排在超时之后，且保持原有相对顺序");
t = factory();
t.load(N("x", "y", "z", "w"), {
  z: { tag: "z", reachable: true, ms: 50 },
  x: { tag: "x", reachable: false, ms: null },
});
t.sort();
check(t.names().join() === "z,x,y,w", "结果：" + t.names().join(" > "));

console.log("\n[3] 已排好序时不再改动（返回 false，避免多余保存）");
t = factory();
t.load(N("b", "d", "a"), {
  a: { reachable: true, ms: 300 },
  b: { reachable: true, ms: 120 },
  d: { reachable: true, ms: 200 },
});
check(t.sort() === false, "顺序未变返回 false");
check(t.names().join() === "b,d,a", "顺序保持：" + t.names().join(" > "));

console.log("\n[4] 通但没耗时（HTTP 通了但没量到）排在失败之前、有耗时之后");
t = factory();
t.load(N("p", "q", "r"), {
  p: { reachable: false, ms: null },
  q: { reachable: true, ms: null, mode: "http", code: 204 },
  r: { reachable: true, ms: 700 },
});
t.sort();
check(t.names().join() === "r,q,p", "结果：" + t.names().join(" > "));

console.log("\n[5] 全部失败时保持原顺序");
t = factory();
t.load(N("m", "n", "o"), {
  m: { reachable: false }, n: { reachable: false }, o: { reachable: false },
});
check(t.sort() === false, "没有可达节点，不重排");
check(t.names().join() === "m,n,o", "顺序保持：" + t.names().join(" > "));

console.log("\n[6] 空列表不炸");
t = factory();
t.load([], {});
check(t.sort() === false, "空列表返回 false");

console.log("\n" + "=".repeat(46));
if (FAILED.length) {
  console.log("失败 " + FAILED.length + " 项：");
  FAILED.forEach((m) => console.log("  - " + m));
  process.exit(1);
}
console.log("全部通过");
