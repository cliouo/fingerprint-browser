// Test only: the receiving side of `fpb open --ext-context`. fpb opens this
// page as options.html#fpbctx=<base64url of the context file>; the page
// erases the hash at once, keeps the decoded object in storage.local and
// reports what storage.local now holds (and the hash left) to the mock
// backend, since Playwright cannot read an extension page.
const MOCK = "http://127.0.0.1:8100";
const PREFIX = "#fpbctx=";

function decode(b64url) {
  const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64 + "===".slice((b64.length + 3) % 4));
  return JSON.parse(new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0))));
}

async function report(body) {
  await fetch(MOCK + "/api/ext-context", {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  });
}

(async () => {
  if (!location.hash.startsWith(PREFIX)) return;
  const raw = location.hash.slice(PREFIX.length);
  history.replaceState(null, "", location.pathname);
  let ctx;
  try {
    ctx = decode(raw);
  } catch (e) {
    await report({error: String(e), hash_after: location.hash});
    return;
  }
  await browser.storage.local.set({ext_context: {...ctx, applied_at: Date.now()}});
  const stored = (await browser.storage.local.get("ext_context")).ext_context;
  document.getElementById("status").textContent = "context stored";
  await report({stored, hash_after: location.hash, href: location.href});
})();
