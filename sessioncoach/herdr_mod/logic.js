// Pure helpers for the sessioncoach panel. No imports from "herdr" or "gpui-kit": small, inert
// functions the mod calls, so their behaviour is easy to reason about, to reuse and to test with
// plain node. The strings follow the language `live --json` reports (`lang`: "es" | "en").

const DAYS = {
  es: ["dom", "lun", "mar", "mié", "jue", "vie", "sáb"],
  en: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
};

// ---- number / time formatting ----

export function fmtTokens(n) {
  if (n == null) return "—";
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
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
// write "<0,1 %" / "<0.1%". Spanish uses a comma decimal and a space before the percent sign.
export function fmtPct(pct, lang) {
  const es = lang === "es";
  if (pct == null) return "—";
  const p = Number(pct);
  if (!Number.isFinite(p)) return "—";
  if (p < 0.1) return es ? "<0,1 %" : "<0.1%";
  if (p < 10) return String(p.toFixed(1)).replace(".", es ? "," : ".") + (es ? " %" : "%");
  return String(Math.round(p)) + (es ? " %" : "%");
}

export function usd(n) {
  if (n == null) return "—";
  return "$" + Number(n).toFixed(2);
}

// Whole dollars for the dim summary line: "309 $" / "$309".
export function usdInt(n, lang) {
  if (n == null) return "—";
  const v = Math.round(Number(n));
  return lang === "es" ? v + " $" : "$" + v;
}

// A rough dollar figure ("~18 $" / "~$18"), two decimals so the small per-turn numbers stay useful.
export function approxUsd(n, lang) {
  if (n == null) return "—";
  const v = Number(n).toFixed(2);
  return lang === "es" ? "~" + v.replace(".", ",") + " $" : "~$" + v;
}

export function cacheLeft(s, now) {
  if (s.cache_expires_at == null) return "—";
  if (s.cache_expires_at < now) return "expired";
  return Math.round((s.cache_expires_at - now) / 60) + " min";
}

// An idle time in min/h/d ("5 min", "46 h", "3 d").
export function fmtIdle(secs) {
  if (secs == null || !Number.isFinite(secs) || secs < 0) return "";
  const m = Math.floor(secs / 60);
  if (m < 60) return m + " min";
  const h = Math.floor(secs / 3600);
  if (h < 48) return h + " h";
  return Math.floor(secs / 86400) + " d";
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
    if (ppc != null && s.turn_usd != null) {
      return (es ? "/compact ahora en " : "/compact now in ") + project + " — "
        + (es ? "cada turno relee " : "each turn re-reads ") + fmtPct(s.turn_usd / ppc, lang)
        + (es ? " de la semana · " : " of the week · ") + expires;
    }
    return (es ? "/compact ahora en " : "/compact now in ") + project + " — "
      + (es ? "cada turno relee " : "each turn re-reads ") + approxUsd(s.turn_usd, lang) + " · " + expires;
  }
  const idle = fmtIdle(now - (s.last_activity != null ? s.last_activity : now));
  const tokens = fmtTokens(s.context_tokens);
  const end = s.clear_handoff
    ? (es ? "· /clear da un traspaso gratis" : "· /clear gives a free handoff")
    : (es ? "· ciérrala o empieza de nuevo" : "· close it or start fresh");
  const head = (es ? "No retomes " : "Don't resume ") + project + " (" + tokens + ", "
    + (es ? "parada " : "idle ") + idle + "): " + (es ? "volver cuesta " : "coming back costs ");
  if (ppc != null && s.rewrite_usd != null) {
    return head + fmtPct(s.rewrite_usd / ppc, lang) + (es ? " de la semana " : " of the week ") + end;
  }
  return head + approxUsd(s.rewrite_usd, lang) + " " + end;
}

// The "NOW" actions: at most three, the biggest-context sessions first by stake (what returning to
// them costs as a share of the week, falling back to the raw rewrite dollars), each with its full
// line. Sessions below `hintMinTokens` are ignored.
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
    const acct = accountFor(view, s);
    const ppc = acct && acct.seven_day ? acct.seven_day.usd_per_pct : null;
    const stake = (ppc != null && s.rewrite_usd != null) ? s.rewrite_usd / ppc
      : (s.rewrite_usd != null ? s.rewrite_usd : 0);
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

// Build the `group sync-herdr` payload from the herdr-space-groups shared value and the local
// spaces. Returns null when the shared value is not a usable object (so the caller does nothing);
// otherwise {groups: [{name, folders}]} — possibly {groups: []} when no group has a local space,
// which clears every imported assignment. Bad groups/spaces are skipped, never fatal.
export function buildSyncPayload(spaceGroups, spacesList) {
  if (spaceGroups == null || typeof spaceGroups !== "object" || Array.isArray(spaceGroups)) return null;
  if (!Array.isArray(spaceGroups.groups)) return null;
  if (spaceGroups.spaces == null || typeof spaceGroups.spaces !== "object" || Array.isArray(spaceGroups.spaces)) return null;
  const byId = new Map();
  for (const g of spaceGroups.groups) {
    if (g && typeof g === "object" && !Array.isArray(g) && g.id != null) byId.set(String(g.id), g);
  }
  const folders = new Map();
  for (const sp of Array.isArray(spacesList) ? spacesList : []) {
    if (!sp || typeof sp !== "object") continue;
    if (sp.machine) continue;  // local machine only
    const gid = spaceGroups.spaces[sp.id];
    // sync-herdr rejects the whole feed for one bad folder: send absolute paths only (POSIX or a drive)
    if (gid == null || typeof sp.folder !== "string" || !/^(\/|[A-Za-z]:[\\/])/.test(sp.folder)) continue;
    const key = String(gid);
    if (!folders.has(key)) folders.set(key, []);
    folders.get(key).push(sp.folder);
  }
  const groups = [];
  for (const [gid, g] of byId) {
    const fs = folders.get(gid);
    if (!fs || !fs.length) continue;
    const name = (g && typeof g.name === "string" && g.name.trim()) || gid;
    groups.push({ name: name.slice(0, 100), folders: fs.slice(0, 1000) });  // sync-herdr's limits
  }
  return { groups };
}
