const DEFAULT_URL = "https://aaronmaddenjk.github.io/oregon-weather-dashboard/";
const input = document.getElementById("url");
const close = document.getElementById("close");
const gh = document.getElementById("gh");
chrome.storage.sync.get({ dashboardUrl: DEFAULT_URL, closeAfterSave: false }, (v) => {
  input.value = v.dashboardUrl;
  close.checked = v.closeAfterSave;
});
// the GitHub key stays in this browser (storage.local, not synced to your Google account)
chrome.storage.local.get({ ghToken: "" }, (v) => { gh.value = v.ghToken; });
document.getElementById("save").addEventListener("click", async () => {
  let u = input.value.trim() || DEFAULT_URL;
  if (!/\/$/.test(u) && !/\.html?$/.test(u)) u += "/";
  const tok = gh.value.trim(), out = document.getElementById("saved");
  if (tok) {   // check it can see the private repo before keeping it
    const r = await fetch("https://api.github.com/repos/aaronmaddenjk/wx-trails", { headers: { Accept: "application/vnd.github+json", Authorization: "Bearer " + tok } }).catch(() => null);
    if (!r || !r.ok) { out.style.color = "#B4441C"; out.textContent = r && r.status === 401 ? "GitHub didn't accept that key" : "That key can't see wx-trails: add it to the key on GitHub"; return; }
  }
  await chrome.storage.sync.set({ dashboardUrl: u, closeAfterSave: close.checked });
  await chrome.storage.local.set({ ghToken: tok, cloudAt: 0 });   // cloudAt 0: re-read your trails for the ✓ marks
  input.value = u; out.style.color = ""; out.textContent = "Saved";
  setTimeout(() => { out.textContent = ""; }, 1500);
});
