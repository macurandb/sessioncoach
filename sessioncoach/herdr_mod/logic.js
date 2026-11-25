// Pure helpers for the sessioncoach panel. No imports from "herdr" or "gpui-kit": small, inert
// functions the mod calls, so their behaviour is easy to reason about, to reuse and to test with
// plain node. The strings follow the language `live --json` reports (`lang`: "es" | "en").

const DAYS = {
  es: ["dom", "lun", "mar", "mié", "jue", "vie", "sáb"],
  en: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
};

// `group sync-herdr`'s bounds, exactly as sessioncoach/projects.py (MAX_GROUPS, MAX_FOLDERS,
// MAX_NAME, MAX_PATH). The panel never truncates: a payload past these is reported, not cut.
const MAX_GROUPS = 200;
const MAX_FOLDERS = 1000;
const MAX_NAME = 100;
const MAX_PATH = 4096;

// ---- number / time formatting ----

export function fmtTokens(n, lang) {
  if (n == null) return "—";
  const es = lang === "es";
  if (n >= 1000000) return (n / 1000000).toFixed(1).replace(".", es ? "," : ".") + "M";
  return Math.round(n / 1000) + "k";
}

export function hhmm(epoch) {
  if (epoch == null) return "";
  const d = new Date(Number(epoch) * 1000);
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

// "ddd HH:MM" with the weekday in the panel language ("jue 21:00" / "Thu 21:00").
export function dddHHMM(epoch, lang) {
  if (epoch == null) return "";
  const d = new Date(Number(epoch) * 1000);
  const days = DAYS[lang === "es" ? "es" : "en"];
  return days[d.getDay()] + " " + String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

// A percentage of the week: one decimal under 10, none above ("0,8 %", "3,2 %", "14 %"); under 0.1
// write "<0,1 %" / "<0.1%". Spanish uses a comma decimal and a space before the percent sign. The
// under-10 / at-least-10 branch is decided on the value rounded to one decimal, so 9.96 -> "10 %".
export function fmtPct(pct, lang) {
  const es = lang === "es";
  if (pct == null) return "—";
  const p = Number(pct);
  if (!Number.isFinite(p)) return "—";
  if (p < 0.1) return es ? "<0,1 %" : "<0.1%";
  const r = Math.round(p * 10) / 10;
  if (r < 10) return String(r.toFixed(1)).replace(".", es ? "," : ".") + (es ? " %" : "%");
  return String(Math.round(r)) + (es ? " %" : "%");
}

// A precise dollar figure, two decimals ("$12.40" / "12,40 $").
export function usd(n, lang) {
  if (n == null) return "—";
  const v = Number(n);
  if (!Number.isFinite(v)) return "—";
  const es = lang === "es";
  return es ? v.toFixed(2).replace(".", ",") + " $" : "$" + v.toFixed(2);
}

// Whole dollars for the dim summary line: "309 $" / "$309".
export function usdInt(n, lang) {
  if (n == null) return "—";
  const es = lang === "es";
  const v = Math.round(Number(n));
  return es ? v + " $" : "$" + v;
}

// A rough dollar figure: whole units at 10 and above ("~18 $" / "~$18"), two decimals below
// ("~0,07 $" / "~$0.07"), so the small per-turn numbers stay useful.
export function approxUsd(n, lang) {
  if (n == null) return "—";
  const v = Number(n);
  if (!Number.isFinite(v)) return "—";
  const es = lang === "es";
  const text = Math.abs(v) >= 10 ? String(Math.round(v)) : v.toFixed(2).replace(".", es ? "," : ".");
  return es ? "~" + text + " $" : "~$" + text;
}

export function cacheLeft(s, now, lang) {
  if (s.cache_expires_at == null) return "—";
  if (s.cache_expires_at < now) return lang === "es" ? "caducada" : "expired";
  return Math.round((s.cache_expires_at - now) / 60) + " min";
}

// An idle time in min/h/d ("5 min", "46 h", "3 d"). A negative idle (a future last_activity,
// a clock skew) is clamped to 0, so it reads "0 min" rather than disappearing.
export function fmtIdle(secs) {
  if (secs == null || !Number.isFinite(secs)) return "";
  const s = Math.max(0, secs);
  const m = Math.floor(s / 60);
  if (m < 60) return m + " min";
  const h = Math.floor(s / 3600);
  if (h < 48) return h + " h";
  return Math.floor(s / 86400) + " d";
}

// A usable USD-per-percentage-point: a finite number > 0. Anything else (null, 0, "8.85", NaN,
// Infinity, negative) means "price unknown", and every consumer falls back to dollars.
export function ppcUsable(v) {
  return typeof v === "number" && Number.isFinite(v) && v > 0;
}

// ---- the verdict (section A) ----

// One of "red" | "amber" | "green" | "none" for the week line, from the seven-day window alone.
export function weekState(seven) {
  const s = seven || {};
  if (s.eta != null && s.resets_at != null && s.eta < s.resets_at) return "red";
  if (s.projected_pct != null && s.projected_pct >= 90) return "amber";
  if (s.projected_pct != null) return "green";
  return "none";
}

// The week line as { state, base, tail }: base is "Semana 53 %" / "Week 53%", tail carries the
// projection and the verdict mark, and is coloured by state.
export function weekVerdict(seven, lang) {
  const es = lang === "es";
  const s = seven || {};
  const state = weekState(s);
  const base = (es ? "Semana " : "Week ") + fmtPct(s.pct, lang);
  let tail = "";
  if (state === "red") {
    tail = (es ? "✗ llegas al 100 % el " : "✗ hits 100% ") + dddHHMM(s.eta, lang);
  } else if (state === "amber") {
    tail = (es ? "a este ritmo " : "at this pace ") + fmtPct(s.projected_pct, lang)
      + (es ? " el " : " by ") + dddHHMM(s.resets_at, lang) + (es ? " ≈ justo" : " ≈ tight");
  } else if (state === "green") {
    tail = (es ? "a este ritmo " : "at this pace ") + fmtPct(s.projected_pct, lang)
      + (es ? " el " : " by ") + dddHHMM(s.resets_at, lang) + (es ? " ✓ vas bien" : " ✓ on track");
  } else if (s.resets_at != null) {
    tail = (es ? "se reinicia el " : "resets ") + dddHHMM(s.resets_at, lang);
  }
  return { state, base, tail };
}

// True when the 5-hour window is on pace to hit 100 % before it resets.
export function fiveHourWarn(five) {
  const f = five || {};
  return f.eta != null && f.resets_at != null && f.eta < f.resets_at;
}

// The red "✗ 100 % a las 12:10" / "✗ 100% at 12:10" suffix for the 5-hour line.
export function fiveHourWarnText(five, lang) {
  const es = lang === "es";
  const f = five || {};
  return (es ? "✗ 100 % a las " : "✗ 100% at ") + hhmm(f.eta);
}

// ---- the action list (section B) ----

function accountFor(view, session) {
  const accounts = Array.isArray(view.accounts) ? view.accounts : [];
  for (const a of accounts) {
    if (a && a.id != null && String(a.id) === String(session.account_id)) return a;
  }
  for (const a of accounts) {
    if (a && a.key != null && String(a.key) === String(session.account)) return a;
  }
  return null;
}

function actionLine(s, acct, now, es, lang) {
  const seven = acct && acct.seven_day ? acct.seven_day : null;
  const ppc = seven ? seven.usd_per_pct : null;
  const project = s.project || "?";
  if (s.cache_expires_at != null && s.cache_expires_at > now) {
    const mins = Math.max(1, Math.round((s.cache_expires_at - now) / 60));
    const expires = es ? ("caduca en " + mins + " min") : ("cache expires in " + mins + " min");
    // A per-turn clause only when there is a turn cost to report; a missing turn_usd drops it.
    let clause = "";
    if (s.turn_usd != null) {
      clause = ppcUsable(ppc)
        ? (es ? "cada turno relee " : "each turn re-reads ") + fmtPct(s.turn_usd / ppc, lang)
          + (es ? " de la semana" : " of the week")
        : (es ? "cada turno relee " : "each turn re-reads ") + approxUsd(s.turn_usd, lang);
    }
    const sep = clause ? " · " : "";
    return (es ? "/compact ahora en " : "/compact now in ") + project + " — " + clause + sep + expires;
  }
  const idle = fmtIdle(now - (s.last_activity != null ? s.last_activity : now));
  const tokens = fmtTokens(s.context_tokens, lang);
  const end = s.clear_handoff === true
    ? (es ? "· al volver, /clear da un traspaso gratis" : "· on return, /clear gives a free handoff")
    : (es ? "· ciérrala o empieza de nuevo" : "· close it or start fresh");
  const head = (es ? "No retomes " : "Don't resume ") + project + " (" + tokens + ", "
    + (es ? "parada " : "idle ") + idle + "): " + (es ? "volver cuesta " : "coming back costs ");
  if (ppcUsable(ppc) && s.rewrite_usd != null) {
    return head + fmtPct(s.rewrite_usd / ppc, lang) + (es ? " de la semana " : " of the week ") + end;
  }
  return head + approxUsd(s.rewrite_usd, lang) + " " + end;
}

// The "NOW" actions: at most three, the biggest-context sessions first, ordered by rewrite_usd
// descending (one unit across accounts; within an account that is the same order as the percentage
// of the week). Sessions below `hintMinTokens` are ignored.
export function buildActions(view, hintMinTokens) {
  const lang = view && view.lang === "es" ? "es" : "en";
  const es = lang === "es";
  const now = view ? view.now : null;
  const min = Number.isFinite(Number(hintMinTokens)) && hintMinTokens > 0 ? Number(hintMinTokens) : 300000;
  const sessions = Array.isArray(view && view.sessions) ? view.sessions : [];
  const rows = [];
  for (const s of sessions) {
    if (!s || typeof s !== "object" || Array.isArray(s)) continue;
    if (s.context_tokens == null || s.context_tokens < min) continue;
    const stake = (s.rewrite_usd != null && Number.isFinite(s.rewrite_usd)) ? s.rewrite_usd : 0;
    rows.push({ session: s, stake });
  }
  rows.sort((a, b) => b.stake - a.stake);
  return rows.slice(0, 3).map((r) => ({
    session: r.session,
    stake: r.stake,
    kind: r.session.cache_expires_at != null && r.session.cache_expires_at > now ? "compact" : "resume",
    line: actionLine(r.session, accountFor(view, r.session), now, es, lang),
  }));
}

// ---- the group sync (section F) ----

// A toast/event links to a terminal only when its session_id names exactly one linked session:
// two sessions sharing an id are ambiguous, so clicking could focus the wrong terminal.
export function unambiguousLink(matches, total) {
  if (total !== 1) return undefined;  // another session shares this id (linked or not), or no count was given
  return Array.isArray(matches) && matches.length === 1 ? matches[0] : undefined;
}

// session_id -> how many sessions of the poll carry it, linked to a terminal or not.
export function sessionIdCounts(sessions) {
  const counts = new Map();
  for (const s of Array.isArray(sessions) ? sessions : []) {
    if (s && typeof s === "object" && s.session_id != null) counts.set(s.session_id, (counts.get(s.session_id) || 0) + 1);
  }
  return counts;
}

// UTF-8 byte length of a string, so the staging payload can be checked against readTextFile's
// 1 MiB limit before it is written (a payload over the limit would otherwise not read back).
export function utf8Bytes(text) {
  let bytes = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text.codePointAt(i);
    if (c > 0xffff) i++;  // a surrogate pair counts once
    bytes += c < 0x80 ? 1 : c < 0x800 ? 2 : c < 0x10000 ? 3 : 4;
  }
  return bytes;
}

// A folder path the panel treats as absolute: a POSIX root, a Windows drive, or a UNC share.
function isAbsoluteFolder(folder) {
  return /^(\/|[A-Za-z]:[\\/]|\\\\)/.test(folder);
}

// One normalisation for group ids and the references to them: a string or a finite number, trimmed.
// null for anything else (arrays, objects, booleans, NaN), which the caller treats as corrupt.
export function normGroupId(v) {
  if (typeof v === "string" || (typeof v === "number" && Number.isFinite(v))) return String(v).trim();
  return null;
}

// A Herdr space entry the sync can trust: an object with a string id, a string folder ("" is that
// machine's home) and a machine that is absent or null (this computer) or a non-empty string (another
// machine), as Herdr's SpaceInfo has it. An empty id or any other machine value is corrupt.
function validSpace(sp) {
  return sp != null && typeof sp === "object" && !Array.isArray(sp) && typeof sp.id === "string"
    && sp.id.trim() !== "" && typeof sp.folder === "string" && (sp.machine == null || typeof sp.machine === "string");
}

// Build the `group sync-herdr` payload from the herdr-space-groups shared value and the local
// spaces. Returns null when the shared value is not usable OR any part of the input is corrupt (a
// bad group, a bad or dangling assignment, a bad space entry, a non-absolute local folder): no sync,
// so a corrupt input can never become an empty feed that clears every imported rule. `{error: <code>}`
// when it is valid but exceeds sync-herdr's limits (no sync, the caller shows an error); otherwise
// `{groups: [{name, folders}]}` — possibly `{groups: []}` when no group has a local space, which
// clears every imported assignment. A local space whose folder is "" (Herdr's home of that machine)
// is skipped: sync-herdr omits the home anyway. Nothing is truncated.
export function buildSyncPayload(spaceGroups, spacesList) {
  if (spaceGroups == null || typeof spaceGroups !== "object" || Array.isArray(spaceGroups)) return null;
  if (!Array.isArray(spaceGroups.groups)) return null;
  if (!Array.isArray(spacesList)) return null;
  if (spaceGroups.spaces == null || typeof spaceGroups.spaces !== "object" || Array.isArray(spaceGroups.spaces)) return null;
  const byId = new Map();
  for (const g of spaceGroups.groups) {
    if (!g || typeof g !== "object" || Array.isArray(g)) return null;
    const key = normGroupId(g.id);
    if (key == null || key === "" || byId.has(key)) return null;  // bad, empty or duplicate id
    byId.set(key, g);
  }
  // Every assignment must name an existing group, checked before filtering to local spaces (so a
  // corrupt assignment on a remote or absent space is caught too). Unassigned spaces are null.
  for (const spid of Object.keys(spaceGroups.spaces)) {
    const gid = spaceGroups.spaces[spid];
    if (gid == null) continue;
    const key = normGroupId(gid);
    if (key == null || !byId.has(key)) return null;
  }
  for (const sp of spacesList) if (!validSpace(sp)) return null;
  const folders = new Map();
  for (const sp of spacesList) {
    if (typeof sp.machine === "string" && sp.machine !== "") continue;  // local machine only
    const gid = spaceGroups.spaces[sp.id];
    if (gid == null) continue;  // a space assigned to no group
    if (sp.folder === "") continue;  // the machine's home: sync-herdr omits it
    if (!isAbsoluteFolder(sp.folder)) return null;
    const key = normGroupId(gid);
    if (!folders.has(key)) folders.set(key, []);
    folders.get(key).push(sp.folder);
  }
  const groups = [];
  for (const [gid, g] of byId) {
    const fs = folders.get(gid);
    if (!fs || !fs.length) continue;
    const name = (typeof g.name === "string" && g.name.trim()) || gid;
    groups.push({ name: String(name), folders: fs });
  }
  if (groups.length > MAX_GROUPS) return { error: "groups" };
  for (const g of groups) {
    if (!g.name.trim() || g.name.length > MAX_NAME) return { error: "name" };
    if (g.folders.length > MAX_FOLDERS) return { error: "folders" };
    for (const f of g.folders) if (f.length > MAX_PATH) return { error: "path" };
  }
  return { groups };
}

// What the panel does with one poll's sync input, as data: "clear" (nothing to do, and any old error
// line goes: sync off, no usable or a corrupt payload, or the payload already applied — an absent and
// a corrupt feed are deliberately the same: nothing is synced and nothing stale is shown), "error" (no
// write, show `error`: over sync-herdr's limits, or a staging file too large to read back) or "write"
// (stage `text` and run sync-herdr).
export function syncDecision(enabled, payload, lastApplied, maxBytes) {
  if (!enabled || payload == null) return { action: "clear" };
  if (payload.error) return { action: "error", error: payload.error };
  const text = JSON.stringify(payload);
  if (text === lastApplied) return { action: "clear" };
  if (utf8Bytes(text) > maxBytes) return { action: "error", error: "size" };
  return { action: "write", text };
}
