const DEFAULT_URL = "http://localhost:8000/";
const input = document.getElementById("url");
chrome.storage.sync.get({ dashboardUrl: DEFAULT_URL }, (v) => { input.value = v.dashboardUrl; });
document.getElementById("save").addEventListener("click", () => {
  let u = input.value.trim() || DEFAULT_URL;
  if (!/\/$/.test(u) && !/\.html?$/.test(u)) u += "/";
  chrome.storage.sync.set({ dashboardUrl: u }, () => {
    input.value = u;
    document.getElementById("saved").textContent = "Saved";
    setTimeout(() => { document.getElementById("saved").textContent = ""; }, 1500);
  });
});
