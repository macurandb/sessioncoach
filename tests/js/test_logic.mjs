// Runs logic.js (the panel's pure helpers) against a few fixed inputs. logic.js is a shipped mod
// file with `export` statements but no package.json to say "type": "module", so this harness copies
// it to a temporary .mjs first — then node treats it as ESM whatever the node version. Exits 0 on
// success (after printing "logic.js ok"); assert throws otherwise.
import { readFileSync, writeFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const logicSrc = join(here, "..", "..", "sessioncoach", "herdr_mod", "logic.js");

const DAYS = {
  es: ["dom", "lun", "mar", "mié", "jue", "vie", "sáb"],
  en: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
};
// the expected "ddd HH:MM" for an epoch, computed in this process's own timezone (the same one the
// helper uses), so the weekday name and the HH:MM format are checked without pinning a timezone.
function expectDdd(epoch, lang) {
  const d = new Date(epoch * 1000);
  const days = DAYS[lang === "es" ? "es" : "en"];
  return days[d.getDay()] + " " + String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

const dir = mkdtempSync(join(tmpdir(), "sessioncoach-logic-"));
try {
  const target = join(dir, "logic.mjs");
  writeFileSync(target, readFileSync(logicSrc, "utf-8"));
  const L = await import(pathToFileURL(target).href);

  // ---- fmtPct: decimals, the comma, the <0.1 rule ----
  assert.equal(L.fmtPct(53, "es"), "53 %");
  assert.equal(L.fmtPct(53, "en"), "53%");
  assert.equal(L.fmtPct(3.2, "es"), "3,2 %");
  assert.equal(L.fmtPct(3.2, "en"), "3.2%");
  assert.equal(L.fmtPct(0.8, "es"), "0,8 %");
  assert.equal(L.fmtPct(14, "es"), "14 %");
  assert.equal(L.fmtPct(14, "en"), "14%");
  assert.equal(L.fmtPct(0.05, "es"), "<0,1 %");
  assert.equal(L.fmtPct(0.05, "en"), "<0.1%");
  assert.equal(L.fmtPct(9.5, "en"), "9.5%");
  assert.equal(L.fmtPct(null, "en"), "—");

  // ---- dddHHMM ----
  const R = 1_800_000_000;
  assert.equal(L.dddHHMM(R, "en"), expectDdd(R, "en"));
  assert.equal(L.dddHHMM(R, "es"), expectDdd(R, "es"));

  // ---- weekState: the four verdict states ----
  assert.equal(L.weekState({ eta: 500, resets_at: 1000, projected_pct: 120 }), "red");
  assert.equal(L.weekState({ eta: 1500, resets_at: 1000, projected_pct: 93 }), "amber");  // eta not early
  assert.equal(L.weekState({ eta: null, projected_pct: 93, resets_at: 1000 }), "amber");
  assert.equal(L.weekState({ eta: null, projected_pct: 78, resets_at: 1000 }), "green");
  assert.equal(L.weekState({ eta: null, projected_pct: null, resets_at: 1000 }), "none");

  // ---- weekVerdict: the full line per state, in both languages ----
  const green = L.weekVerdict({ pct: 53, resets_at: R, projected_pct: 78, eta: null }, "en");
  assert.equal(green.state, "green");
  assert.equal(green.base, "Week 53%");
  assert.equal(green.tail, "at this pace 78% by " + expectDdd(R, "en") + " ✓ on track");

  const greenEs = L.weekVerdict({ pct: 53, resets_at: R, projected_pct: 78, eta: null }, "es");
  assert.equal(greenEs.state, "green");
  assert.equal(greenEs.base, "Semana 53 %");
  assert.equal(greenEs.tail, "a este ritmo 78 % el " + expectDdd(R, "es") + " ✓ vas bien");

  const amber = L.weekVerdict({ pct: 91, resets_at: R, projected_pct: 93, eta: null }, "en");
  assert.equal(amber.state, "amber");
  assert.equal(amber.tail, "at this pace 93% by " + expectDdd(R, "en") + " ≈ tight");

  const red = L.weekVerdict({ pct: 120, resets_at: R, projected_pct: 120, eta: R - 3600 }, "en");
  assert.equal(red.state, "red");
  assert.equal(red.tail, "✗ hits 100% " + expectDdd(R - 3600, "en"));

  const none = L.weekVerdict({ pct: 53, resets_at: R, projected_pct: null, eta: null }, "es");
  assert.equal(none.state, "none");
  assert.equal(none.tail, "se reinicia el " + expectDdd(R, "es"));

  // ---- buildActions: ordering, the limit, es/en, the null-usd_per_pct fallback ----
  const now = 1_800_000_000;
  const accounts = [
    { id: "a1", key: "one", seven_day: { usd_per_pct: 10 } },
    { id: "a2", key: "two", seven_day: { usd_per_pct: null } },
  ];
  const sessions = [
    { pid: 1, session_id: "s1", project: "api", account_id: "a1", context_tokens: 908000,
      last_activity: now - 46 * 3600, cache_expires_at: now - 100, rewrite_usd: 32, turn_usd: 8,
      clear_handoff: false },
    { pid: 2, session_id: "s2", project: "shop", account_id: "a1", context_tokens: 335000,
      last_activity: now - 60, cache_expires_at: now + 12 * 60, rewrite_usd: 8, turn_usd: 8,
      clear_handoff: true },
    { pid: 3, session_id: "s3", project: "big", account_id: "a2", context_tokens: 400000,
      last_activity: now - 3600, cache_expires_at: now - 100, rewrite_usd: 1.0, turn_usd: 0.5,
      clear_handoff: true },
    { pid: 4, session_id: "s4", project: "tiny", account_id: "a1", context_tokens: 42000,
      last_activity: now - 60, cache_expires_at: now + 3600, rewrite_usd: 0.01, turn_usd: 0.0,
      clear_handoff: true },
    { pid: 5, session_id: "s5", project: "more", account_id: "a1", context_tokens: 500000,
      last_activity: now - 7200, cache_expires_at: now + 3600, rewrite_usd: 0.5, turn_usd: 0.5,
      clear_handoff: true },
  ];
  const view = { lang: "en", now, accounts, sessions };
  const acts = L.buildActions(view, 300000);
  assert.equal(acts.length, 3);  // capped at three, and the 42k session is below the hint
  assert.deepEqual(acts.map((a) => a.session.project), ["api", "big", "shop"]);  // by stake desc
  assert.deepEqual(acts.map((a) => a.kind), ["resume", "resume", "compact"]);
  assert.equal(acts[0].line, "Don't resume api (908k, idle 46 h): coming back costs 3.2% of the week · close it or start fresh");
  assert.equal(acts[1].line, "Don't resume big (400k, idle 1 h): coming back costs ~$1.00 · /clear gives a free handoff");
  assert.equal(acts[2].line, "/compact now in shop — each turn re-reads 0.8% of the week · cache expires in 12 min");

  const viewEs = { lang: "es", now, accounts, sessions };
  const actsEs = L.buildActions(viewEs, 300000);
  assert.equal(actsEs[0].line, "No retomes api (908k, parada 46 h): volver cuesta 3,2 % de la semana · ciérrala o empieza de nuevo");
  assert.equal(actsEs[1].line, "No retomes big (400k, parada 1 h): volver cuesta ~1,00 $ · /clear da un traspaso gratis");

  // ---- buildSyncPayload ----
  assert.equal(L.buildSyncPayload(null, []), null);
  assert.equal(L.buildSyncPayload({ groups: "x" }, []), null);
  assert.equal(L.buildSyncPayload({ groups: [], spaces: null }, []), null);
  const payload = L.buildSyncPayload(
    { groups: [{ id: "acme", name: "Acme" }, { id: "globex", name: "Globex" }],
      spaces: { sp1: "acme", sp2: "acme", sp3: "globex" } },
    [{ id: "sp1", folder: "/home/you/a", machine: false },
     { id: "sp2", folder: "relative/path", machine: false },   // not absolute: skipped
     { id: "sp3", folder: "C:\\work\\g", machine: false },
     { id: "sp4", folder: "/home/you/remote", machine: true }], // remote machine: skipped
  );
  assert.deepEqual(payload, { groups: [{ name: "Acme", folders: ["/home/you/a"] },
                                        { name: "Globex", folders: ["C:\\work\\g"] }] });
  assert.deepEqual(L.buildSyncPayload({ groups: [], spaces: {} }, []), { groups: [] });

  console.log("logic.js ok");
} finally {
  rmSync(dir, { recursive: true, force: true });
}
