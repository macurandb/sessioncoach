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
function expectHHMM(epoch) {
  const d = new Date(epoch * 1000);
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

const dir = mkdtempSync(join(tmpdir(), "sessioncoach-logic-"));
try {
  const target = join(dir, "logic.mjs");
  writeFileSync(target, readFileSync(logicSrc, "utf-8"));
  const L = await import(pathToFileURL(target).href);

  // ---- fmtPct: decimals, the comma, the <0.1 rule, and the 9.96 -> 10 rounding ----
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
  assert.equal(L.fmtPct(9.96, "en"), "10%");   // rounded to 1 decimal first, not "10.0%"
  assert.equal(L.fmtPct(9.96, "es"), "10 %");
  assert.equal(L.fmtPct(null, "en"), "—");

  // ---- money formats (item 15) and localisation (item 14) ----
  assert.equal(L.usd(12.4, "es"), "12,40 $");
  assert.equal(L.usd(12.4, "en"), "$12.40");
  assert.equal(L.usdInt(1535, "es"), "1535 $");
  assert.equal(L.usdInt(1535, "en"), "$1535");
  assert.equal(L.approxUsd(18, "es"), "~18 $");
  assert.equal(L.approxUsd(18, "en"), "~$18");
  assert.equal(L.approxUsd(0.07, "es"), "~0,07 $");
  assert.equal(L.approxUsd(0.07, "en"), "~$0.07");
  assert.equal(L.approxUsd(9.99, "en"), "~$9.99");
  assert.equal(L.fmtTokens(1200000, "es"), "1,2M");
  assert.equal(L.fmtTokens(1200000, "en"), "1.2M");
  assert.equal(L.fmtTokens(500000, "es"), "500k");
  assert.equal(L.cacheLeft({ cache_expires_at: 100 }, 200, "es"), "caducada");
  assert.equal(L.cacheLeft({ cache_expires_at: 100 }, 200, "en"), "expired");
  assert.equal(L.cacheLeft({ cache_expires_at: 200 + 42 * 60 }, 200, "es"), "42 min");

  // ---- ppcUsable: only a finite number > 0 lets the % form run ----
  assert.equal(L.ppcUsable(8.85), true);
  assert.equal(L.ppcUsable(0), false);
  assert.equal(L.ppcUsable(null), false);
  assert.equal(L.ppcUsable("8.85"), false);
  assert.equal(L.ppcUsable(NaN), false);
  assert.equal(L.ppcUsable(-1), false);

  // ---- dddHHMM ----
  const R = 1_800_000_000;
  assert.equal(L.dddHHMM(R, "en"), expectDdd(R, "en"));
  assert.equal(L.dddHHMM(R, "es"), expectDdd(R, "es"));

  // ---- fmtIdle: a negative idle (future last_activity) clamps to 0 ----
  assert.equal(L.fmtIdle(-3600), "0 min");
  assert.equal(L.fmtIdle(0), "0 min");
  assert.equal(L.fmtIdle(5 * 60), "5 min");
  assert.equal(L.fmtIdle(46 * 3600), "46 h");
  assert.equal(L.fmtIdle(3 * 86400), "3 d");
  assert.equal(L.fmtIdle(null), "");
  assert.equal(L.fmtIdle(NaN), "");

  // ---- the 5 h line (fiveHourWarn / fiveHourWarnText) ----
  assert.equal(L.fiveHourWarn({ eta: 500, resets_at: 1000 }), true);
  assert.equal(L.fiveHourWarn({ eta: 1500, resets_at: 1000 }), false);
  assert.equal(L.fiveHourWarn({ eta: 500 }), false);  // no reset: not "before it resets"
  assert.equal(L.fiveHourWarn({}), false);
  assert.equal(L.fiveHourWarnText({ eta: R - 600 }, "es"), "✗ 100 % a las " + expectHHMM(R - 600));
  assert.equal(L.fiveHourWarnText({ eta: R - 600 }, "en"), "✗ 100% at " + expectHHMM(R - 600));

  // ---- unambiguousLink: a toast links only when the id names exactly one session ----
  assert.equal(L.unambiguousLink(undefined), undefined);
  assert.equal(L.unambiguousLink(null), undefined);
  assert.equal(L.unambiguousLink([]), undefined);
  assert.equal(L.unambiguousLink([{ address: "a" }, { address: "b" }]), undefined);  // two sessions, one id
  assert.deepEqual(L.unambiguousLink([{ address: "a" }]), { address: "a" });

  // ---- utf8Bytes: the staging payload's size in bytes ----
  assert.equal(L.utf8Bytes(""), 0);
  assert.equal(L.utf8Bytes("abc"), 3);
  assert.equal(L.utf8Bytes("é"), 2);
  assert.equal(L.utf8Bytes("a".repeat(1024)), 1024);

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

  // ---- buildActions: ordering by rewrite_usd, the limit, es/en, the fallbacks ----
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
  assert.deepEqual(acts.map((a) => a.session.project), ["api", "shop", "big"]);  // by rewrite_usd desc
  assert.deepEqual(acts.map((a) => a.kind), ["resume", "compact", "resume"]);
  assert.equal(acts[0].line, "Don't resume api (908k, idle 46 h): coming back costs 3.2% of the week · close it or start fresh");
  assert.equal(acts[1].line, "/compact now in shop — each turn re-reads 0.8% of the week · cache expires in 12 min");
  assert.equal(acts[2].line, "Don't resume big (400k, idle 1 h): coming back costs ~$1.00 · on return, /clear gives a free handoff");

  const viewEs = { lang: "es", now, accounts, sessions };
  const actsEs = L.buildActions(viewEs, 300000);
  assert.equal(actsEs[0].line, "No retomes api (908k, parada 46 h): volver cuesta 3,2 % de la semana · ciérrala o empieza de nuevo");
  assert.equal(actsEs[1].line, "/compact ahora en shop — cada turno relee 0,8 % de la semana · caduca en 12 min");
  assert.equal(actsEs[2].line, "No retomes big (400k, parada 1 h): volver cuesta ~1,00 $ · al volver, /clear da un traspaso gratis");

  // ---- a clear_handoff of the string "false" is not true -> the no-handoff ending ----
  const stringFalse = L.buildActions({ lang: "en", now, accounts,
    sessions: [{ pid: 1, session_id: "s1", project: "api", account_id: "a1", context_tokens: 400000,
      last_activity: now - 3600, cache_expires_at: now - 100, rewrite_usd: 10, clear_handoff: "false" }] }, 300000);
  assert.equal(stringFalse[0].line,
    "Don't resume api (400k, idle 1 h): coming back costs 1.0% of the week · close it or start fresh");

  // ---- the missing-fields fixture: an older CLI with no new fields must degrade, not throw ----
  const bareView = { lang: "en", now,
    accounts: [{ id: "a1", key: "one", seven_day: { pct: 50 } }],  // no usd_per_pct
    sessions: [
      { pid: 1, session_id: "s1", project: "api", account_id: "a1", context_tokens: 400000,
        last_activity: now - 60, cache_expires_at: now + 720, rewrite_usd: 8 },   // no turn_usd, no clear_handoff
      { pid: 2, session_id: "s2", project: "shop", account_id: "a1", context_tokens: 500000,
        last_activity: now - 7200, cache_expires_at: now - 100, rewrite_usd: 9 },  // no usd_per_pct -> dollars
    ],
  };
  const bare = L.buildActions(bareView, 300000);
  assert.deepEqual(bare.map((a) => a.session.project), ["shop", "api"]);
  assert.equal(bare[0].line, "Don't resume shop (500k, idle 2 h): coming back costs ~$9.00 · close it or start fresh");
  assert.equal(bare[1].line, "/compact now in api — cache expires in 12 min");  // no turn_usd: the per-turn clause is dropped

  // ---- buildSyncPayload: valid, empty, corrupt -> null, oversized -> {error} ----
  assert.equal(L.buildSyncPayload(null, []), null);
  assert.equal(L.buildSyncPayload({ groups: "x" }, []), null);
  assert.equal(L.buildSyncPayload({ groups: [], spaces: null }, []), null);
  // a group entry without a string/number id is corrupt -> null (not an empty feed)
  assert.equal(L.buildSyncPayload({ groups: [{ name: "Acme" }], spaces: {} }, []), null);
  // a space pointing at a group id that does not exist is corrupt -> null
  assert.equal(L.buildSyncPayload(
    { groups: [{ id: "acme", name: "Acme" }], spaces: { sp1: "ghost" } },
    [{ id: "sp1", folder: "/home/you/a", machine: false }]), null);
  // a local space with a non-absolute folder is corrupt -> null
  assert.equal(L.buildSyncPayload(
    { groups: [{ id: "acme", name: "Acme" }], spaces: { sp1: "acme" } },
    [{ id: "sp1", folder: "relative/path", machine: false }]), null);

  const payload = L.buildSyncPayload(
    { groups: [{ id: "acme", name: "Acme" }, { id: "globex", name: "Globex" }],
      spaces: { sp1: "acme", sp3: "globex" } },
    [{ id: "sp1", folder: "/home/you/a", machine: false },
     { id: "sp3", folder: "C:\\work\\g", machine: false },
     { id: "sp4", folder: "/home/you/remote", machine: true }], // remote machine: skipped
  );
  assert.deepEqual(payload, { groups: [{ name: "Acme", folders: ["/home/you/a"] },
                                        { name: "Globex", folders: ["C:\\work\\g"] }] });
  assert.deepEqual(L.buildSyncPayload({ groups: [], spaces: {} }, []), { groups: [] });

  // ---- oversized payloads: reported (error code), never truncated ----
  const manyGroups = [], manyGroupSpaces = {}, manyGroupList = [];
  for (let i = 0; i < 201; i++) {
    manyGroups.push({ id: "g" + i, name: "G" + i });
    manyGroupSpaces["sp" + i] = "g" + i;
    manyGroupList.push({ id: "sp" + i, folder: "/f" + i, machine: false });
  }
  assert.deepEqual(L.buildSyncPayload({ groups: manyGroups, spaces: manyGroupSpaces }, manyGroupList), { error: "groups" });
  assert.deepEqual(L.buildSyncPayload(
    { groups: [{ id: "acme", name: "A".repeat(101) }], spaces: { sp1: "acme" } },
    [{ id: "sp1", folder: "/x", machine: false }]), { error: "name" });
  assert.deepEqual(L.buildSyncPayload(
    { groups: [{ id: "acme", name: "Acme" }], spaces: { sp1: "acme" } },
    [{ id: "sp1", folder: "/" + "x".repeat(4096), machine: false }]), { error: "path" });
  const manyFolders = Array.from({ length: 1001 }, (_, i) => "/f" + i);
  const manySpaces = {};
  manyFolders.forEach((f, i) => { manySpaces["sp" + i] = "acme"; });
  assert.deepEqual(L.buildSyncPayload(
    { groups: [{ id: "acme", name: "Acme" }], spaces: manySpaces },
    manyFolders.map((f, i) => ({ id: "sp" + i, folder: f, machine: false }))), { error: "folders" });

  // ---- spacesList not an array -> null ----
  assert.equal(L.buildSyncPayload({ groups: [], spaces: {} }, null), null);
  assert.equal(L.buildSyncPayload({ groups: [], spaces: {} }, "x"), null);

  // ---- every spaces entry must reference an existing group, even a space not in the local list ----
  assert.equal(L.buildSyncPayload(
    { groups: [{ id: "acme", name: "Acme" }], spaces: { remote: "ghost" } },
    [{ id: "sp1", folder: "/x", machine: false }]), null);
  // ... a non-null value of any type that is not a group id is corrupt
  assert.equal(L.buildSyncPayload(
    { groups: [{ id: "acme", name: "Acme" }], spaces: { sp1: 3 } },
    [{ id: "sp1", folder: "/x", machine: false }]), null);

  // ---- group ids: empty after trim, non-finite number, duplicate after normalising ----
  assert.equal(L.buildSyncPayload({ groups: [{ id: "  ", name: "Acme" }], spaces: {} }, []), null);
  assert.equal(L.buildSyncPayload({ groups: [{ id: NaN, name: "Acme" }], spaces: {} }, []), null);
  assert.equal(L.buildSyncPayload({ groups: [{ id: Infinity, name: "Acme" }], spaces: {} }, []), null);
  assert.equal(L.buildSyncPayload({ groups: [{ id: "acme" }, { id: "acme" }], spaces: {} }, []), null);
  assert.equal(L.buildSyncPayload({ groups: [{ id: 1 }, { id: "1" }], spaces: {} }, []), null);  // 1 and "1" collide

  // ---- UNC folders are absolute; a final name that trims empty falls back to the (valid) id ----
  assert.deepEqual(L.buildSyncPayload(
    { groups: [{ id: "acme", name: "Acme" }], spaces: { sp1: "acme" } },
    [{ id: "sp1", folder: "\\\\server\\share\\work", machine: false }]),
    { groups: [{ name: "Acme", folders: ["\\\\server\\share\\work"] }] });
  // a group whose name is only whitespace keeps its id as the name
  assert.deepEqual(L.buildSyncPayload(
    { groups: [{ id: "acme", name: "   " }], spaces: { sp1: "acme" } },
    [{ id: "sp1", folder: "/x", machine: false }]), { groups: [{ name: "acme", folders: ["/x"] }] });

  // ---- round 5: corrupt space entries and references never become an empty feed ----
  const G = { groups: [{ id: "acme", name: "Acme" }], spaces: { s: "acme" } };
  assert.equal(L.buildSyncPayload(G, [null]), null);
  assert.equal(L.buildSyncPayload(G, [{ id: 7, folder: "/x" }]), null);            // id not a string
  assert.equal(L.buildSyncPayload(G, [{ id: "s" }]), null);                         // no folder
  assert.equal(L.buildSyncPayload(G, [{ id: "s", folder: "/x", machine: 3 }]), null);
  assert.equal(L.buildSyncPayload({ groups: [{ id: "acme" }], spaces: { s: ["acme"] } }, []), null);  // array ref
  assert.equal(L.buildSyncPayload({ groups: [{ id: "acme" }], spaces: { s: { id: "acme" } } }, []), null);
  assert.equal(L.buildSyncPayload({ groups: [{ id: "acme" }], spaces: { s: true } }, []), null);
  // ids and references share one normalisation: " acme " matches " acme "
  assert.deepEqual(L.buildSyncPayload(
    { groups: [{ id: " acme ", name: "Acme" }], spaces: { s: " acme " } }, [{ id: "s", folder: "/x" }]),
    { groups: [{ name: "Acme", folders: ["/x"] }] });
  // the home ("") of this machine is skipped, the other space still syncs
  assert.deepEqual(L.buildSyncPayload(
    { groups: [{ id: "acme", name: "Acme" }], spaces: { h: "acme", s: "acme" } },
    [{ id: "h", folder: "", machine: null }, { id: "s", folder: "/x", machine: null }]),
    { groups: [{ name: "Acme", folders: ["/x"] }] });
  // a valid empty input still clears
  assert.deepEqual(L.buildSyncPayload({ groups: [], spaces: {} }, []), { groups: [] });

  // ---- syncDecision: the poll's sync step as data ----
  const P = { groups: [{ name: "Acme", folders: ["/x"] }] };
  const PT = JSON.stringify(P);
  assert.deepEqual(L.syncDecision(false, P, null, 1 << 20), { action: "clear" });       // sync off
  assert.deepEqual(L.syncDecision(true, null, null, 1 << 20), { action: "clear" });     // no usable / corrupt
  assert.deepEqual(L.syncDecision(true, P, PT, 1 << 20), { action: "clear" });          // back to the applied one
  assert.deepEqual(L.syncDecision(true, { error: "groups" }, null, 1 << 20), { action: "error", error: "groups" });
  assert.deepEqual(L.syncDecision(true, P, null, 10), { action: "error", error: "size" });  // too big: no write
  assert.deepEqual(L.syncDecision(true, P, "older", 1 << 20), { action: "write", text: PT });

  // ---- toast links: two sessions with one id, only one linked -> no link ----
  const counts = L.sessionIdCounts([{ session_id: "same", pid: 1 }, { session_id: "same", pid: 2 }, { session_id: "x" }, null]);
  assert.equal(counts.get("same"), 2);
  assert.equal(counts.get("x"), 1);
  assert.equal(L.unambiguousLink([{ address: "a" }], counts.get("same")), undefined);
  assert.deepEqual(L.unambiguousLink([{ address: "a" }], counts.get("x")), { address: "a" });
  assert.deepEqual(L.unambiguousLink([{ address: "a" }]), { address: "a" });  // old callers: no total

  console.log("logic.js ok");
} finally {
  rmSync(dir, { recursive: true, force: true });
}
