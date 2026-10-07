const DEFAULT_URL = "https://aaronmaddenjk.github.io/oregon-weather-dashboard/";
const input = document.getElementById("url");
const close = document.getElementById("close");
chrome.storage.sync.get({ dashboardUrl: DEFAULT_URL, closeAfterSave: false }, (v) => {
  input.value = v.dashboardUrl;
  close.checked = v.closeAfterSave;
});
document.getElementById("save").addEventListener("click", () => {
  let u = input.value.trim() || DEFAULT_URL;
  if (!/\/$/.test(u) && !/\.html?$/.test(u)) u += "/";
  chrome.storage.sync.set({ dashboardUrl: u, closeAfterSave: close.checked }, () => {
    input.value = u;
    document.getElementById("saved").textContent = "Saved";
    setTimeout(() => { document.getElementById("saved").textContent = ""; }, 1500);
  });
});
