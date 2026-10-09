// Trail list: links you collected (a notepad file or a paste). "Open and save next 20" opens the next 20
// unsaved links as background tabs (0.7 s apart) and hands them to background.js, which saves each one
// (one at a time) and closes its tab; a tab that fails stays open. One batch per click, never continuing
// by itself (owner's choice, 2026-10-08). Rows turn Saved / Failed as background.js reports back.
// State in chrome.storage.local: trailList = [{u, opened, failed}].
const $ = (id) => document.getElementById(id);
const BATCH = 20;
let list = [], saved = {}, running = null;   // running: {total, done, ok, timer}

// same key as background.js: host + path, /explore/ dropped, no trailing slash
function linkKey(u) {
  try { const x = new URL(u); return x.hostname.replace(/^www\./, "") + x.pathname.replace(/^\/explore\//, "/").replace(/\/$/, ""); }
  catch (e) { return ""; }
}
function parse(text) {
  return (text.match(/https?:\/\/[^\s"'<>,]+/g) || []).map((u) => u.replace(/[).\]]+$/, ""));
}
async function load() {
  const v = await chrome.storage.local.get({ trailList: [], saved: {} });
  list = v.trailList; saved = v.saved;
  render();
}
function store() { return chrome.storage.local.set({ trailList: list }); }
const isSaved = (it) => !!saved[linkKey(it.u)];
const todo = () => list.filter((it) => !it.opened && !it.failed && !isSaved(it));
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function render() {
  const n = list.length, s = list.filter(isSaved).length, f = list.filter((it) => it.failed && !isSaved(it)).length, left = todo().length;
  $("sum").innerHTML = n ? `<span><b>${n}</b> links</span><span><b>${s}</b> saved</span><span><b>${f}</b> failed</span><span><b>${left}</b> to go</span>` : "";
  $("next").textContent = running ? "Saving…" : left ? `Open and save next ${Math.min(BATCH, left)}` : "Nothing left to save";
  $("next").disabled = !!running || !left;
  $("prog").textContent = running ? `${running.done} of ${running.total} done · ${running.ok} saved` : "";
  $("list").innerHTML = list.map((it) => {
    const st = isSaved(it) ? "saved" : it.failed ? "failed" : it.opened ? (running ? "saving" : "opened") : "";
    return `<li class="${st}"><span class="st ${st}">${st || "—"}</span><a href="${esc(it.u)}" target="_blank" rel="noopener">${esc(it.u.replace(/^https?:\/\/(www\.)?/, ""))}</a>`
      + (st === "failed" ? `<span class="why">${esc(it.failed)}</span>` : "") + "</li>";
  }).join("");
}
function add(text) {
  const have = new Set(list.map((it) => linkKey(it.u)));
  let added = 0;
  parse(text).forEach((u) => { const k = linkKey(u); if (k && !have.has(k)) { have.add(k); list.push({ u, opened: false }); added++; } });
  store(); render();
  $("msg").textContent = added ? `Added ${added} link${added > 1 ? "s" : ""}.` : "No new links found there.";
}
function finish(note) {
  if (!running) return;
  clearTimeout(running.timer);
  const r = running; running = null;
  store(); render();
  $("msg").textContent = note || `Batch done: ${r.ok} of ${r.total} saved${r.total - r.ok ? ", " + (r.total - r.ok) + " failed (their tabs are still open)" : ""}.`;
}

$("add").addEventListener("click", () => { add($("paste").value); $("paste").value = ""; });
$("file").addEventListener("change", async function () { const f = this.files[0]; this.value = ""; if (f) add(await f.text()); });
$("next").addEventListener("click", async () => {
  const batch = todo().slice(0, BATCH);
  if (!batch.length || running) return;
  running = { total: batch.length, done: 0, ok: 0, timer: null };
  $("msg").textContent = "Opening tabs…";
  render();
  const items = [];
  for (const it of batch) {   // a short pause between tabs, about the pace of middle-clicking
    const t = await chrome.tabs.create({ url: it.u, active: false });
    it.opened = true; delete it.failed;
    items.push({ tabId: t.id, url: it.u });
    await new Promise((r) => setTimeout(r, 700));
  }
  await store();
  $("msg").textContent = "Saving each one; tabs close as they're saved. You can keep using Chrome meanwhile.";
  running.timer = setTimeout(() => finish("Stopped waiting after 10 minutes; anything unfinished can be retried."), 10 * 60 * 1000);
  render();
  chrome.runtime.sendMessage({ type: "saveTabs", items }).catch(() => finish("Couldn't reach the extension's background; reload it at chrome://extensions."));
});
$("reset").addEventListener("click", () => { list.forEach((it) => { if (!isSaved(it)) { it.opened = false; delete it.failed; } }); store(); render(); });
$("clear").addEventListener("click", () => {
  if ($("clear").dataset.sure !== "1") { $("clear").dataset.sure = "1"; $("clear").textContent = "Click again to clear"; return; }
  list = []; store(); render(); $("clear").dataset.sure = ""; $("clear").textContent = "Clear the list";
});
// background.js reports each trail of the batch
chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || msg.type !== "saveResult") return;
  const it = list.find((x) => x.u === msg.url) || list.find((x) => linkKey(x.u) === linkKey(msg.url));
  if (it && !msg.ok) it.failed = msg.error || "couldn't save it";
  if (it && msg.ok) delete it.failed;
  if (running && it && it.opened) {
    running.done++; if (msg.ok) running.ok++;
    if (running.done >= running.total) finish(); else render();
  } else { store(); render(); }
});
// a trail saved anywhere: its row turns Saved
chrome.storage.onChanged.addListener((ch, area) => { if (area === "local" && ch.saved) { saved = ch.saved.newValue || {}; render(); } });
chrome.runtime.sendMessage({ type: "refreshSaved" }).catch(() => {});   // pull your trails.json for the Saved marks
load();
