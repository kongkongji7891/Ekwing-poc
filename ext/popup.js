const $ = (id) => document.getElementById(id);

const REQUIRED = ["uid", "EKWUID", "accessId"];
const OPTIONAL = ["IS_SCHOOL_LOGIN", "client_type", "from_type", "href"];
const JUNK = /^(qimo_|zg_|pageViewNum$)/;

document.addEventListener("DOMContentLoaded", async () => {
  const { token = "", port = "7788" } = await chrome.storage.local.get(["token", "port"]);
  $("token").value = token;
  $("port").value = port;
  $("go").addEventListener("click", send);
});

function say(msg, cls) {
  const el = $("out");
  el.textContent = msg;
  el.className = cls || "";
}

async function grabCookies() {
  const seen = new Map();
  const add = (c) => {
    if (!c || !c.value) return;
    const old = seen.get(c.name);
    if (!old || c.value.length > old.value.length) seen.set(c.name, c);
  };

  (await chrome.cookies.getAll({ domain: "ekwing.com" })).forEach(add);
  for (const url of ["https://www.ekwing.com", "https://api.ekwing.com"]) {
    try {
      (await chrome.cookies.getAll({ url })).forEach(add);
    } catch (e) {
      /* 某个域拿不到就跳过 */
    }
  }

  const all = [...seen.values()].sort((a, b) => a.name.localeCompare(b.name));
  return all;
}

async function send() {
  const token = $("token").value.trim();
  const port = $("port").value.trim() || "7788";
  await chrome.storage.local.set({ token, port });

  if (!token) return say("先填令牌（wing serve 会打印）", "bad");

  $("go").disabled = true;
  say("正在读取 cookie …");

  let list;
  try {
    list = await grabCookies();
  } catch (e) {
    $("go").disabled = false;
    return say("读不到 cookie：" + e.message + "\n（检查插件权限是否允许 ekwing.com）", "bad");
  }

  // 取插件抓到的真实请求：优先 api 子域，其次最近一条
  let caps = [];
  try {
    caps = (await chrome.storage.session.get(["caps"])).caps || [];
  } catch (e) {
    /* storage.session 不可用 */
  }
  caps = caps.filter((c) => Date.now() - c.ts < 60 * 60 * 1000);
  const cap = caps[0] || null;
  const capHeaders = cap ? cap.headers : null;
  const capCookie =
    (capHeaders && (capHeaders.Cookie || capHeaders.cookie)) || "";
  const capNames = capHeaders
    ? Object.keys(capHeaders).filter((k) => !/^(cookie|host|content-length)$/i.test(k))
    : [];

  const names = list.map((c) => c.name);
  const missing = REQUIRED.filter((n) => !names.includes(n));
  const keep = list.filter((c) => !JUNK.test(c.name));
  const built = keep.map((c) => `${c.name}=${c.value}`).join("; ");
  const cookie = capCookie || built;

  const summary =
    `读到 ${list.length} 条，将发送 ${capCookie ? "原始 Cookie" : keep.length + " 条"}\n` +
    `关键：${REQUIRED.map((n) => (names.includes(n) ? n + " ✓" : n + " ✗")).join("  ")}\n` +
    (cap
      ? `已捕获 ${caps.length} 条，全部交给 wing 了\n最新一条：${cap.type} ${cap.method} ${cap.url.replace(/^https?:\/\//, "").slice(0, 40)}`
      : "⚠ 一条都没抓到。重载插件后必须刷新翼课网页面，请求才会被记录");

  if (missing.length) {
    $("go").disabled = false;
    return say(summary + `\n\n缺 ${missing.join(", ")} —— 先在浏览器里登录翼课网，再点一次。`, "bad");
  }

  try {
    const r = await fetch(`http://127.0.0.1:${port}/ingest`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Wing-Token": token },
      body: JSON.stringify({
        cookie,
        names,
        optional: OPTIONAL,
        ua: navigator.userAgent,
        headers: capHeaders,
        apiUrl: cap ? cap.url : null,
        apiMethod: cap ? cap.method : null,
        seeList: caps.map((c) => c.method + " " + c.url.replace(/^https?:\/\//, "")),
        caps: caps.map((c) => ({
          url: c.url,
          method: c.method,
          type: c.type,
          headers: c.headers,
        })),
      }),
    });
    const j = await r.json().catch(() => ({}));
    $("go").disabled = false;

    if (!r.ok) {
      return say(summary + "\n\n本机拒绝：" + (j.error || r.status), "bad");
    }
    if (j.ok) {
      return say(summary + "\n\n✓ 已交给 wing" + (j.user ? "\n" + j.user : ""), "ok");
    }
    return say(summary + "\n\n发过去了，但会话无效：" + (j.detail || ""), "bad");
  } catch (e) {
    $("go").disabled = false;
    say(
      summary +
        "\n\n连不上本机服务：" + e.message +
        "\n是不是 wing serve 没在跑？端口对不上也会这样。",
      "bad"
    );
  }
}
