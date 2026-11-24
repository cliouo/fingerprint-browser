// Test extension: does its work in tabs it opens itself, like many scraping or
// automation extensions: an active runner tab (tabs.create), a results POST to
// the mock backend, then the tab is removed. Three rounds, so a launcher that
// mistakes "no tab Playwright tracks" for "window closed" would end the browser.
//
// CI rewrites these two lines to test a proxy bypass: MOCK becomes a LAN
// address (proxied unless bypassed; 127.0.0.1 never is), RUNNER_URL a public page.
const MOCK = "http://127.0.0.1:8100";
const RUNNER_URL = MOCK + "/runner";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, body, bearer) {
  const res = await fetch(MOCK + path, {
    method: "POST",
    headers: {"Content-Type": "application/json", ...(bearer ? {Authorization: "Bearer " + bearer} : {})},
    body: JSON.stringify(body),
  });
  return res.json();
}

async function round(n, bearer) {
  const tab = await browser.tabs.create({url: `${RUNNER_URL}?round=${n}`, active: true});
  await sleep(4000);
  await api("/api/results", {page_url: `${RUNNER_URL}?round=${n}`,
    items: [{title: `runner-ext item ${n}`, url: `${MOCK}/item/${n}`}]}, bearer);
  await browser.tabs.remove(tab.id);
}

(async () => {
  await sleep(8000);
  const login = await api("/api/login", {username: "demo", password: "demo-pass", client: "runner-ext"});
  const bearer = login.data && login.data.access_token;
  for (let n = 1; n <= 3; n++) {
    try { await round(n, bearer); } catch (e) { console.error("round", n, e); }
    await sleep(10000);
  }
})();
