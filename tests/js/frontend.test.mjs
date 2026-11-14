/**
 * 前端纯函数单元测试（node:test，零外部依赖）。
 *
 * 背景：前端是单文件 web/index.html，此前**没有任何测试**，
 * 以至于 esc() 这种被调用 5 次却从未定义的函数长期存在（运行时 ReferenceError）。
 *
 * 做法：从真实的 index.html 中提取 <script> 块，在最小 DOM 桩里执行，
 * 然后断言纯函数行为 —— 测的是真实文件，不是副本。
 *
 * 运行：node --test tests/js/
 */

import { test, describe } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "..", "..");
const indexPath = path.join(repoRoot, "bedrock_server_manager", "web", "index.html");

function makeEl(tag = "div") {
  const el = {
    tagName: String(tag).toUpperCase(),
    style: {},
    dataset: {},
    classList: { add() {}, remove() {}, contains: () => false, toggle() {} },
    children: [],
    innerHTML: "",
    textContent: "",
    value: "",
    checked: false,
    scrollTop: 0,
    scrollHeight: 0,
    appendChild(c) {
      this.children.push(c);
      return c;
    },
    removeChild() {},
    remove() {},
    setAttribute() {},
    getAttribute: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    removeEventListener() {},
    focus() {},
    click() {},
    insertAdjacentHTML() {},
  };
  // 真实 DOM 里 innerHTML='' 会清空子节点；桩必须同样处理，
  // 否则跨用例的 children 会累积，导致断言读到上一个用例的元素。
  let html = "";
  Object.defineProperty(el, "innerHTML", {
    get: () => html,
    set: (v) => {
      html = String(v ?? "");
      if (html === "") el.children.length = 0;
    },
  });
  return el;
}

/** 加载 index.html 的全部 script 块到一个沙箱里，返回该沙箱。 */
function loadFrontend() {
  const html = fs.readFileSync(indexPath, "utf8");
  const blocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  assert.ok(blocks.length >= 1, "index.html 中未找到 <script> 块");

  const registry = new Map();
  const byId = (id) => {
    if (!registry.has(id)) registry.set(id, makeEl());
    return registry.get(id);
  };

  // 定时器全部做成惰性桩：只测纯函数，不需要真的跑轮询/动画，
  // 否则页面里的 setInterval 会让 node --test 永远等不到事件循环清空。
  let timerSeq = 0;
  const pending = new Map();
  const fakeSetTimeout = (fn) => {
    const id = ++timerSeq;
    pending.set(id, fn);
    return id;
  };
  const fakeClear = (id) => pending.delete(id);

  const sandbox = {
    console,
    document: {
      documentElement: { style: { setProperty() {}, getPropertyValue: () => "" } },
      body: makeEl("body"),
      getElementById: byId,
      querySelector: (sel) => {
        if (sel === 'meta[name="api-token"]') return { content: "0123456789abcdef0123456789abcdef" };
        return makeEl();
      },
      querySelectorAll: () => [],
      createElement: (t) => makeEl(t),
      addEventListener() {},
      createTreeWalker: () => ({ nextNode: () => null }),
    },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    navigator: { clipboard: { writeText: () => Promise.resolve() }, language: "zh-CN", userAgent: "node" },
    location: { href: "http://127.0.0.1:19100/", reload() {} },
    fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}), blob: () => Promise.resolve({}) }),
    EventSource: class {
      constructor() {}
      close() {}
    },
    URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
    setTimeout: fakeSetTimeout,
    clearTimeout: fakeClear,
    setInterval: fakeSetTimeout,
    clearInterval: fakeClear,
    MutationObserver: class {
      observe() {}
      disconnect() {}
    },
    alert() {},
    confirm: () => true,
    prompt: () => "",
    requestAnimationFrame: (f) => fakeSetTimeout(f),
    Blob: class {},
    FormData: class {},
    get __byId() {
      return byId;
    },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  const ctx = vm.createContext(sandbox);
  blocks.forEach((code, i) => {
    vm.runInContext(code, ctx, { filename: `index-block${i}.js` });
  });
  return ctx;
}

const ctx = loadFrontend();

// index.html 里大量状态是顶层 `let` 声明 —— vm 中这类绑定属于脚本的词法作用域，
// 不会挂到 context 对象上。要读写它们，必须在同一 realm 内求值。
const evalIn = (code) => vm.runInContext(code, ctx, { filename: "eval.js" });
const setVar = (name, value) => {
  ctx.__tmp = value;
  evalIn(`${name} = __tmp;`);
};
const getVar = (expr) => JSON.parse(evalIn(`JSON.stringify(${expr})`));

describe("HTML 转义（XSS 防护）", () => {
  test("esc 已定义（回归：曾被调用 5 次但从未定义）", () => {
    assert.equal(typeof ctx.esc, "function", "esc 未定义会直接抛 ReferenceError");
  });

  test("esc 中和脚本标签", () => {
    const out = ctx.esc("<img src=x onerror=alert(1)>");
    assert.equal(out, "&lt;img src=x onerror=alert(1)&gt;");
    assert.ok(!out.includes("<img"));
  });

  test("esc 转义引号（可安全放进属性值）", () => {
    assert.equal(ctx.esc(`a"b'c`), "a&quot;b&#39;c");
  });

  test("esc 处理 null/undefined 不抛错", () => {
    assert.equal(ctx.esc(null), "");
    assert.equal(ctx.esc(undefined), "");
  });

  test("escapeHtml 转义 & 与尖括号", () => {
    assert.equal(ctx.escapeHtml("<b>&</b>"), "&lt;b&gt;&amp;&lt;/b&gt;");
  });
});

describe("控制台日志高亮", () => {
  test("先转义再加高亮 span，玩家聊天无法注入", () => {
    const out = ctx.highlightLog('<script>alert(1)</script>');
    assert.ok(!out.includes("<script>"), "未转义就直接拼 HTML 了");
    assert.ok(out.includes("&lt;script&gt;"));
  });

  test("ERROR 关键字被标红", () => {
    const out = ctx.highlightLog("ERROR something failed");
    assert.ok(out.includes("color:#f85149"), "ERROR 未高亮");
  });

  test("普通文本原样保留", () => {
    assert.ok(ctx.highlightLog("hello world").includes("hello world"));
  });
});

describe("包卡片渲染", () => {
  test("包名被转义，不拼进 onclick 属性", () => {
    const evil = { name: '"><img src=x onerror=alert(1)>', version: "1.0", enabled: true };
    const html = ctx.packCardHtml("resource", evil);
    assert.ok(!html.includes("<img"), "包名未经转义进入了 HTML");
    assert.ok(!html.includes('onclick="togglePack('), "仍在拼接 onclick 属性");
  });

  test("改用 data-* 属性 + 事件委托", () => {
    const html = ctx.packCardHtml("resource", { name: "MyPack", version: "1.0", enabled: true });
    assert.ok(html.includes("data-pack-name="));
    assert.ok(html.includes('data-pack-type="resource"'));
    assert.ok(html.includes("onTogglePack(this)"));
  });

  test("启用状态影响按钮文案与 data 值", () => {
    const on = ctx.packCardHtml("behavior", { name: "B", version: "1", enabled: true });
    const off = ctx.packCardHtml("behavior", { name: "B", version: "1", enabled: false });
    assert.ok(on.includes('data-pack-enable="0"'), "已启用时按钮应表示禁用");
    assert.ok(off.includes('data-pack-enable="1"'));
  });
});

describe("计划任务渲染", () => {
  test("公告消息被转义（防持久化 XSS）", () => {
    setVar("scheduledTasks", [
      { id: "t1", type: "announce", schedule_type: "daily", time: "04:00", enabled: true, message: "<img src=x>" },
    ]);
    const ctxBox = ctx.__byId("scheduledList");
    ctx.renderScheduledTasks();
    assert.ok(ctxBox.innerHTML.length > 0, "没有渲染出内容");
    assert.ok(!ctxBox.innerHTML.includes("<img"), "公告消息未转义");
    assert.ok(ctxBox.innerHTML.includes("&lt;img"), "应保留转义后的文本");
  });
});

describe("控制台搜索", () => {
  test("无关键字时还原完整日志", () => {
    setVar("consolePlain", "line1\nline2");
    ctx.__byId("consoleSearch").value = "";
    ctx.filterConsole();
    const box = ctx.__byId("consoleBox");
    assert.ok(box.innerHTML.includes("line1"));
    assert.ok(box.innerHTML.includes("line2"));
  });

  test("有关键字时只保留匹配行", () => {
    setVar("consolePlain", "alpha\nbeta\ngamma");
    ctx.__byId("consoleSearch").value = "beta";
    ctx.filterConsole();
    const box = ctx.__byId("consoleBox");
    assert.ok(box.innerHTML.includes("beta"));
    assert.ok(!box.innerHTML.includes("alpha"));
  });

  test("无匹配时给出提示而不是空白", () => {
    setVar("consolePlain", "alpha");
    ctx.__byId("consoleSearch").value = "zzz";
    ctx.filterConsole();
    assert.ok(ctx.__byId("consoleBox").innerHTML.includes("没有匹配"));
  });

  test("搜索不破坏高亮转义", () => {
    setVar("consolePlain", "ERROR test");
    ctx.__byId("consoleSearch").value = "ERROR";
    ctx.filterConsole();
    assert.ok(ctx.__byId("consoleBox").innerHTML.includes("color:#f85149"));
  });
});

describe("确认弹窗（曾未定义导致 SSL 确认框抛错）", () => {
  test("showConfirm 已定义并触发回调", () => {
    assert.equal(typeof ctx.showConfirm, "function");
    let received = null;
    ctx.showConfirm("标题", "内容", (ok) => {
      received = ok;
    });
    assert.equal(ctx.__byId("confirmTitle").textContent, "标题");
    assert.equal(ctx.__byId("confirmMsg").textContent, "内容");
    // 模拟点击"确定"
    ctx.__byId("confirmOk").onclick();
    assert.equal(received, true);
  });

  test("取消回调 false", () => {
    let received = null;
    ctx.showConfirm("t", "m", (ok) => {
      received = ok;
    });
    ctx.__byId("confirmCancel").onclick();
    assert.equal(received, false);
  });
});

describe("配置渲染（高级模式曾调用未定义函数）", () => {
  test("renderConfig 已定义", () => {
    assert.equal(typeof ctx.renderConfig, "function");
  });

  test("按元数据渲染全部字段", () => {
    const meta = [
      ["server-name", "服务器名称", "text", "Dedicated Server"],
      ["gamemode", "游戏模式", "choice", ["survival", "creative"]],
      ["allow-cheats", "允许作弊", "bool", "true"],
      ["max-players", "最大玩家数", "int", "10"],
    ];
    ctx.renderConfig({ "server-name": "Mine", gamemode: "creative" }, meta);
    const grid = ctx.__byId("cfgGrid");
    assert.equal(grid.children.length, 4, "字段数量不符");
    const vals = getVar("cfgVals");
    assert.equal(vals["server-name"], "Mine");
    assert.equal(vals["gamemode"], "creative");
  });

  test("choice 类型保留当前值（即使不在选项里）", () => {
    ctx.renderConfig({ gamemode: "custom-mode" }, [["gamemode", "游戏模式", "choice", ["survival"]]]);
    const select = ctx.__byId("cfgGrid").children[0].children[1];
    assert.equal(select.value, "custom-mode", "未知当前值丢失了");
  });

  test("bool 类型归一化为 true/false", () => {
    ctx.renderConfig({ "allow-cheats": "TRUE" }, [["allow-cheats", "允许作弊", "bool", "true"]]);
    const select = ctx.__byId("cfgGrid").children[0].children[1];
    assert.equal(select.value, "true");
  });
});
