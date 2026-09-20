import fs from "node:fs";

const data = JSON.parse(fs.readFileSync("public/outlets.js", "utf8").replace(/^window\.OUTLETS=/, "").replace(/;\s*$/, ""));
let prev = { fails: {} };
try { prev = JSON.parse(fs.readFileSync("public/status.json", "utf8")); } catch {}

const urls = [...new Set(data.languages.flatMap(l => l.outlets.map(o => o[1])))];
const UA = "Mozilla/5.0 (compatible; IndiaNewsHubLinkCheck/1.0)";
// 401/403/429 = site is up but blocks bots, so not counted as broken
const OK = s => s < 400 || [401, 403, 405, 429, 999].includes(s);

async function alive(url) {
  for (const method of ["HEAD", "GET"]) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 20000);
    try {
      const r = await fetch(url, { method, redirect: "follow", signal: ctrl.signal, headers: { "user-agent": UA } });
      if (OK(r.status) && !(method === "HEAD" && r.status === 405)) return true;
    } catch {} finally { clearTimeout(t); }
  }
  return false;
}

const fails = {};
const broken = [];
let i = 0;
await Promise.all(Array.from({ length: 10 }, async () => {
  while (i < urls.length) {
    const u = urls[i++];
    if (await alive(u)) continue;
    fails[u] = (prev.fails?.[u] || 0) + 1;
    broken.push(`${fails[u]}x ${u}`);
  }
}));

fs.writeFileSync("public/status.json", JSON.stringify({ updated: new Date().toISOString(), fails }, null, 1));
console.log(`Checked ${urls.length}. Failing: ${broken.length}`);
broken.forEach(b => console.log(" ", b));
console.log("Links failing 2 weeks in a row are hidden from the site automatically.");
