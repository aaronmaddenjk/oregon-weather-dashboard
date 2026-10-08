// Trail list: links you collected (a notepad file or a paste), opened a batch at a time as background
// tabs when you click "Open next". Saving stays one trail per click on a page you're looking at
// (the extension button / Alt+Shift+S); this page only opens tabs and shows which links are saved.
// State in chrome.storage.local: trailList = [{u, opened}], batch size in trailBatch.
const $ = (id) => document.getElementById(id);
let list = [], saved = {};

// same key as background.js: host + path, /explore/ dropped, no trailing slash
function linkKey(u) {
  try { const x = new URL(u); return x.hostname.replace(/^www\./, "") + x.pathname.replace(/^\/explore\//, "/").replace(/\/$/, ""); }
  catch (e) { return ""; }
}
function parse(text) {
  return (text.match(/https?:\/\/[^\s"'<>,]+/g) || []).map((u) => u.replace(/[).\]]+$/, ""));
}
async function load() {
  const v = await chrome.storage.local.get({ trailList: [], trailBatch: 10, saved: {} });
  list = v.trailList; saved = v.saved; $("batch").value = String(v.trailBatch);
  render();
}
function store() { return chrome.storage.local.set({ trailList: list }); }
const isSaved = (it) => !!saved[linkKey(it.u)];

function render() {
  const n = list.length, s = list.filter(isSaved).length, o = list.filter((it) => it.opened && !isSaved(it)).length;
  const left = list.filter((it) => !it.opened && !isSaved(it)).length, b = +$("batch").value;
  $("sum").innerHTML = n ? `<span><b>${n}</b> links</span><span><b>${s}</b> saved</span><span><b>${o}</b> opened, not saved yet</span><span><b>${left}</b> to open</span>` : "";
  $("next").textContent = left ? `Open next ${Math.min(b, left)}` : "Nothing left to open";
  $("next").disabled = !left;
  $("list").innerHTML = list.map((it) => {
    const st = isSaved(it) ? "saved" : it.opened ? "opened" : "";
    return `<li class="${st}"><span class="st ${st}">${st || "—"}</span><a href="${it.u.replace(/"/g, "&quot;")}" target="_blank" rel="noopener">${it.u.replace(/^https?:\/\/(www\.)?/, "").replace(/</g, "&lt;")}</a></li>`;
  }).join("");
}
function add(text) {
  const have = new Set(list.map((it) => linkKey(it.u)));
  let added = 0;
  parse(text).forEach((u) => { const k = linkKey(u); if (k && !have.has(k)) { have.add(k); list.push({ u, opened: false }); added++; } });
  store(); render();
  $("msg").textContent = added ? `Added ${added} link${added > 1 ? "s" : ""}.` : "No new links found there.";
}

$("add").addEventListener("click", () => { add($("paste").value); $("paste").value = ""; });
$("file").addEventListener("change", async function () { const f = this.files[0]; this.value = ""; if (f) add(await f.text()); });
$("batch").addEventListener("change", () => { chrome.storage.local.set({ trailBatch: +$("batch").value }); render(); });
$("next").addEventListener("click", async () => {
  const todo = list.filter((it) => !it.opened && !isSaved(it)).slice(0, +$("batch").value);
  $("next").disabled = true;
  for (const it of todo) {   // a short pause between tabs, about the pace of middle-clicking
    await chrome.tabs.create({ url: it.u, active: false });
    it.opened = true;
    await new Promise((r) => setTimeout(r, 700));
  }
  await store(); render();
  $("msg").textContent = `Opened ${todo.length} tab${todo.length > 1 ? "s" : ""}. Save each one with Alt+Shift+S, then come back for the next batch.`;
});
$("reset").addEventListener("click", () => { list.forEach((it) => { it.opened = false; }); store(); render(); });
$("clear").addEventListener("click", () => {
  if ($("clear").dataset.sure !== "1") { $("clear").dataset.sure = "1"; $("clear").textContent = "Click again to clear"; return; }
  list = []; store(); render(); $("clear").dataset.sure = ""; $("clear").textContent = "Clear the list";
});
// a trail saved in another tab: its row turns Saved
chrome.storage.onChanged.addListener((ch, area) => { if (area === "local" && ch.saved) { saved = ch.saved.newValue || {}; render(); } });
chrome.runtime.sendMessage({ type: "refreshSaved" }).catch(() => {});   // pull your trails.json for the Saved marks
load();
