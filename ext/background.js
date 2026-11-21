// 观察 *.ekwing.com 的请求，留最近 20 条。
// 丢掉：埋点域名 zgd.*、图片/字体/样式/脚本 —— 它们会挤掉真正有用的请求。
// 只读取请求头，不修改、不拦截、不上传。
const MAX = 20;
const SKIP_TYPE = new Set(["image", "font", "stylesheet", "script"]);
const SKIP_HOST = /(^|\.)zgd\.ekwing\.com$/i;

function useful(d) {
  if (SKIP_TYPE.has(d.type)) return false;
  try {
    if (SKIP_HOST.test(new URL(d.url).hostname)) return false;
  } catch (e) {
    return false;
  }
  return /^https?:\/\/[\w\-.]*ekwing\.com\//i.test(d.url);
}

chrome.webRequest.onSendHeaders.addListener(
  async (d) => {
    if (!useful(d)) return;
    const h = {};
    for (const x of d.requestHeaders || []) h[x.name] = x.value;
    const item = { url: d.url, method: d.method, type: d.type, headers: h, ts: Date.now() };
    const cur = (await chrome.storage.session.get(["caps"])).caps || [];
    const next = cur.filter((x) => !(x.url === item.url && x.method === item.method));
    next.unshift(item);
    await chrome.storage.session.set({ caps: next.slice(0, MAX) });
  },
  { urls: ["*://*.ekwing.com/*"] },
  ["requestHeaders", "extraHeaders"]
);
