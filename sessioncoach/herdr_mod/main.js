// A Herdr panel for sessioncoach: the running Claude sessions, per-account plan
// limits and per-client (group) spend, from `sessioncoach live --json`. Read-only
// except for the group sync it feeds back: it reads one shared key, writes one
// staging file, runs `sessioncoach group sync-herdr`, and keeps its own fold state
// in one shared key. Nothing else.

import { View, div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import {
  exec, hc, readTextFile, writeTextFile, appHome, checking, theme, spaces, agents, run,
  shared, setShared, notify,
} from "herdr";
import { buildSyncPayload, groupSessionsByAccount } from "./logic.js";

const DEFAULTS = {
  command: "sessioncoach", refresh_s: 30, toasts: true, hint_min_tokens: 300000,
  sync_herdr_groups: true,
};
const TOASTED = "sessioncoach.toasted";
const FOLDED = "sessioncoach.folded";
const SYNC_FILE = "/mods/sessioncoach/.groups-sync.json";

// The panel's own strings, in the language `live --json` reports (`lang`).
const LANG = {
  es: {
    in_window: "en esta ventana", this_week: "esta semana", at_pace: "a este ritmo",
    today: "Hoy", sessions: "sesiones", recent: "Alertas recientes", none: "No hay ninguna sesión de Claude Code en marcha",
    other: "Otras sesiones", groups: "Grupos", today_hdr: "hoy", week_hdr: "semana", compact: "⚠ /compact o /handoff",
  },
  en: {
    in_window: "in this window", this_week: "this week", at_pace: "at this pace",
    today: "Today", sessions: "sessions", recent: "Recent alerts", none: "No Claude Code session running",
    other: "Other sessions", groups: "Groups", today_hdr: "today", week_hdr: "week", compact: "⚠ /compact or /handoff",
  },
};

// ---- number / time formatting ----

function fmtTokens(n) {
  if (n == null) return "—";
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
  return Math.round(n / 1000) + "k";
}

function cacheLeft(s, now) {
  if (s.cache_expires_at == null) return "—";
  if (s.cache_expires_at < now) return "expired";
  return Math.round((s.cache_expires_at - now) / 60) + " min";
}

function usd(n) {
  if (n == null) return "—";
  return "$" + Number(n).toFixed(2);
}

function hhmm(epoch) {
  if (epoch == null) return "";
  const d = new Date(Number(epoch) * 1000);
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

function dddhhmm(epoch) {
  if (epoch == null) return "";
  const d = new Date(Number(epoch) * 1000);
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  return days[d.getDay()] + " " + String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

function pctColor(t, pct) {
  if (pct >= 90) return t.blocked;
  if (pct >= 75) return t.working;
  return t.foreground;
}

export default class SessionCoach extends View {
  init(_props, cx) {
    this.cfg = { ...DEFAULTS };
    this.view = null;             // last good `live --json` document
    this.error = null;            // one-line error, shown above the last good data
    this.syncError = null;        // one-line error from the group sync, or null when it is fine
    this.lastAppliedJson = null;  // the last payload the CLI applied successfully (exit 0)
    this.cursor = null;           // in memory only, never in shared
    this.firstPoll = true;        // the first poll after this start lists the backlog and toasts nothing
    this.events = [];             // events received, in memory, oldest first
    this.sessionLink = {};        // session_id -> { address, space }
    this.addrPid = new Map();     // agent address -> foreground pid
    this.addrCwd = new Map();     // agent address -> foreground cwd
    this.addrSpace = new Map();   // agent address -> space id
    this.polling = false;
    cx.spawn(async (cx) => {
      if (checking()) return;  // herdr-app check must not run anything
      this.cfg = { ...DEFAULTS, ...(await this.readConfig()) };
      cx.timer.every(this.intervalMs(), (cx) => this.refresh(cx));
      this.refresh(cx);
    });
  }

  intervalMs() {
    const s = Number(this.cfg.refresh_s);
    const secs = Number.isFinite(s) && s > 0 ? s : DEFAULTS.refresh_s;
    return Math.max(5, Math.min(600, secs)) * 1000;  // poll gently, timers run while hidden
  }

  langTable() {
    return LANG[this.view && this.view.lang === "es" ? "es" : "en"];
  }

  async readConfig() {
    try {
      const text = await readTextFile(appHome() + "/mods/sessioncoach/config.json");
      if (!text) return {};
      const obj = JSON.parse(text);
      return obj && typeof obj === "object" && !Array.isArray(obj) ? obj : {};
    } catch { return {}; }
  }

  refresh(cx) {
    cx.spawn(async (cx) => {
      if (checking() || this.polling) return;
      this.polling = true;
      try {
        await this.updateAgentPids();
        const data = await this.pollOnce();
        if (data) {
          const first = this.firstPoll;
          this.firstPoll = false;
          const lower = this.cursor != null && data.cursor != null && data.cursor < this.cursor;
          this.view = data;
          this.linkSessions(data);
          this.accumulateEvents(data.events);
          if (data.cursor != null && Number.isFinite(Number(data.cursor))) this.cursor = data.cursor;
          this.toastNew(data.events, first, lower);
        }
        await this.syncGroups();
      } finally {
        this.polling = false;
      }
      cx.notify();
    });
  }

  // address -> foreground pid / cwd, refreshed on each poll (never from render).
  async updateAgentPids() {
    const list = agents();
    const pids = new Map();
    const cwds = new Map();
    const spacesById = new Map();
    await Promise.all(list.map(async (a) => {
      spacesById.set(a.address, a.space);
      try {
        const r = await hc(["get", a.address, "--json"]);
        const info = JSON.parse(r.stdout);
        const fg = info && info.foreground;
        if (fg && typeof fg.pid === "number") pids.set(a.address, fg.pid);
        if (fg && typeof fg.cwd === "string") cwds.set(a.address, fg.cwd);
      } catch { /* a terminal gone mid-poll: skip it */ }
    }));
    this.addrPid = pids;
    this.addrCwd = cwds;
    this.addrSpace = spacesById;
  }

  async pollOnce() {
    const command = typeof this.cfg.command === "string" && this.cfg.command ? this.cfg.command : DEFAULTS.command;
    const args = [];
    if (Array.isArray(this.cfg.args_prefix)) args.push(...this.cfg.args_prefix.map(String));
    args.push("live", "--json");
    if (this.cursor != null && Number.isFinite(Number(this.cursor))) args.push("--since", String(this.cursor));
    if (Array.isArray(this.cfg.args_extra)) args.push(...this.cfg.args_extra.map(String));
    let result;
    try {
      result = await exec(command, args, { timeout: 15000 });
    } catch (e) {  // exec rejects when the command cannot start: not installed
      this.error = "sessioncoach not found: run `sessioncoach herdr-install` again (command: " + command + ")";
      return null;
    }
    if (result.code !== 0) {
      this.error = "sessioncoach live failed (exit " + result.code + ")";
      return null;
    }
    let data;
    try {
      data = JSON.parse(result.stdout);
    } catch (e) {
      this.error = "sessioncoach live: invalid JSON";
      return null;
    }
    if (!data || data.version !== 1) {
      this.error = "sessioncoach live: unknown version " + (data && data.version);
      return null;
    }
    this.error = null;
    return data;
  }

  // Feed the herdr-space-groups assignments back into sessioncoach. The full set is sent every time
  // ({"groups": []} clears them); the last applied payload is remembered only after the CLI exits 0,
  // so a failed sync is retried on the next poll. `exec` has no stdin, so the payload is staged in a
  // file whose current contents are passed as the write's `expected` value.
  async syncGroups() {
    if (this.cfg.sync_herdr_groups === false) return;
    try {
      const payload = buildSyncPayload(shared("space-groups"), spaces());
      if (payload == null) return;  // no usable shared state: nothing to do
      const text = JSON.stringify(payload);
      if (text === this.lastAppliedJson) return;  // already applied
      const path = appHome() + SYNC_FILE;
      await this.stagePayload(path, text);
      const command = typeof this.cfg.command === "string" && this.cfg.command ? this.cfg.command : DEFAULTS.command;
      const args = [];
      if (Array.isArray(this.cfg.args_prefix)) args.push(...this.cfg.args_prefix.map(String));
      args.push("group", "sync-herdr", "--json", path);
      const result = await exec(command, args, { timeout: 15000 });
      if (result.code === 0) {
        this.lastAppliedJson = text;
        this.syncError = null;
      } else {
        this.syncError = "group sync failed (exit " + result.code + ")";
      }
    } catch (e) {
      this.syncError = "group sync: " + (e && e.message ? e.message : String(e));
    }
  }

  // Write the payload, passing the staging file's current text as `expected`; on a stale-snapshot
  // rejection, re-read once and retry.
  async stagePayload(path, text) {
    let expected = null;
    try { expected = await readTextFile(path); } catch { expected = null; }
    try {
      await writeTextFile(path, text, expected);
    } catch (e) {
      try { expected = await readTextFile(path); } catch { expected = null; }
      await writeTextFile(path, text, expected);
    }
  }

  // Match each session to a terminal: pid first, then cwd only when exactly one
  // agent has it (two Claude tabs in one repo are common).
  linkSessions(data) {
    const links = {};
    for (const s of Array.isArray(data.sessions) ? data.sessions : []) {
      if (!s || typeof s !== "object") continue;  // a bad session must not break the poll
      let address = null;
      for (const [a, p] of this.addrPid) if (p === s.pid) { address = a; break; }
      if (!address && typeof s.cwd === "string" && s.cwd) {
        let found = null;
        let count = 0;
        for (const [a, cwd] of this.addrCwd) if (cwd === s.cwd) { found = a; count += 1; }
        if (count === 1) address = found;
      }
      if (address && s.session_id != null) links[s.session_id] = { address, space: this.addrSpace.get(address) };
    }
    this.sessionLink = links;
  }

  accumulateEvents(events) {
    if (!Array.isArray(events) || !events.length) return;
    const known = new Set(this.events.map((e) => e.id));
    for (const e of events) if (e && !known.has(e.id)) { this.events.push(e); known.add(e.id); }
    this.events.sort((a, b) => ((a.id || 0) - (b.id || 0)));
    if (this.events.length > 100) this.events = this.events.slice(-50);
  }

  // Toast each new warning once, across every copy of the mod, through one shared key.
  toastNew(events, first, lower) {
    if (this.cfg.toasts === false || first || lower) return;
    if (!Array.isArray(events) || !events.length) return;
    const toasted = Number(shared(TOASTED)) || 0;
    const candidates = events.filter((e) => e && e.level === "warning" && (e.id || 0) > toasted);
    if (!candidates.length) return;
    for (const e of candidates) {
      const link = this.sessionLink[e.session_id];
      notify({
        title: e.title || "",
        body: e.body || "",
        level: "warning",
        action: link ? { name: "agent.focus", args: { terminal: link.address } } : undefined,
      });
    }
    setShared(TOASTED, Math.max(toasted, ...candidates.map((e) => e.id || 0)));
  }

  render(cx) {
    try {
      const t = theme();
      const root = v_flex().size_full().min_h(0).bg(t.chrome).text_size(13).overflow_y_scrollbar().mode("always");
      if (checking()) return root;  // static empty state during herdr-app check
      if (!this.view) {
        if (this.error) return root.p(10).child(div().text_color(t.blocked).child(this.error));
        return root;  // first poll still in flight
      }
      const children = [];
      if (this.error) children.push(div().px(12).pt(8).pb(2).text_size(12).text_color(t.blocked).child(this.error));
      if (this.syncError) children.push(div().px(12).pt(8).pb(2).text_size(12).text_color(t.blocked).child(this.syncError));
      const sessions = Array.isArray(this.view.sessions) ? this.view.sessions : [];
      if (!sessions.length) {
        children.push(div().px(12).pt(10).pb(4).text_size(12).text_color(t.dim).child(this.langTable().none));
      }
      const activeSpace = (spaces().find((s) => s.active) || {}).id;
      const grouped = groupSessionsByAccount(this.view.accounts, this.view.sessions);
      for (const card of grouped.cards) children.push(this.accountCard(t, card, activeSpace));
      if (grouped.other.length) children.push(this.otherBlock(t, grouped.other, activeSpace));
      children.push(...this.groupRows(t));
      children.push(...this.alertRows(t));
      return root.children(children);
    } catch (e) {  // never throw out of render
      return v_flex().size_full().bg(theme().chrome).p(10).child(div().text_color(theme().dim).child("sessioncoach"));
    }
  }

  accountPct(a) {
    const f = a && a.five_hour ? a.five_hour.pct : null;
    const s = a && a.seven_day ? a.seven_day.pct : null;
    if (f == null && s == null) return null;
    return Math.max(f == null ? 0 : f, s == null ? 0 : s);
  }

  dot(t, pct) {
    const color = pct == null ? t.dim : pct >= 90 ? t.blocked : pct >= 75 ? t.working : t.done;
    return div().flex_none().w(8).h(8).rounded_full().bg(color);
  }

  bar(t, pct) {
    const filled = Math.max(0, Math.min(10, Math.round((pct || 0) / 10)));
    const color = pct == null ? t.raised : pct >= 90 ? t.blocked : pct >= 75 ? t.working : t.done;
    const cells = [];
    for (let i = 0; i < 10; i++) cells.push(div().flex_none().w(6).h(8).rounded(1).bg(i < filled ? color : t.raised));
    return h_flex().flex_none().gap(1).children(cells);
  }

  foldState() {
    const f = shared(FOLDED);
    return f && typeof f === "object" && !Array.isArray(f) ? f : {};
  }

  toggleFold(key) {
    const folded = this.foldState();
    folded[key] = !folded[key];
    setShared(FOLDED, { ...folded });
  }

  windowRow(t, L, win, isWeek) {
    const w = win || {};
    const pct = w.pct;
    const caption = isWeek
      ? usd(w.window_usd) + " " + L.this_week
        + (w.projected_pct != null ? " · " + L.at_pace + " " + Math.round(w.projected_pct) + "%" : "")
      : usd(w.window_usd) + " " + L.in_window;
    return v_flex().pt(2)
      .child(h_flex().gap(8).items_center()
        .child(this.bar(t, pct))
        .child(div().flex_none().w(40).text_color(pct == null ? t.dim : pctColor(t, pct))
          .child(pct == null ? "—" : Math.round(pct) + "%"))
        .child(div().flex_none().text_color(t.dim).child(isWeek ? dddhhmm(w.resets_at) : hhmm(w.resets_at))))
      .child(div().text_size(12).text_color(t.muted).child(caption));
  }

  accountCard(t, card, activeSpace) {
    const L = this.langTable();
    const a = card.account;
    const key = a.id;
    const folded = this.foldState();
    const isFolded = Boolean(folded[key]);
    const out = v_flex().id("acct-" + key).px(12).py(6);
    out.child(
      h_flex().gap(6).items_center().cursor_pointer().on_click(() => this.toggleFold(key))
        .child(this.dot(t, this.accountPct(a)))
        .child(div().flex_1().min_w(0).truncate().font_medium().text_color(t.foreground)
          .child((a.label || "?") + " · " + (a.plan || "")))
        .child(div().flex_none().text_color(t.dim).child(isFolded ? "›" : "˅")),
    );
    if (!isFolded) {
      out.child(this.windowRow(t, L, a.five_hour, false));
      out.child(this.windowRow(t, L, a.seven_day, true));
      out.child(div().pt(2).text_size(12).text_color(t.muted)
        .child(L.today + " " + usd(a.today_usd) + " · " + (a.sessions || 0) + " " + L.sessions));
      for (const s of this.orderedSessions(card.sessions, activeSpace)) out.child(this.sessionRow(t, s));
    }
    return out;
  }

  otherBlock(t, sessions, activeSpace) {
    const L = this.langTable();
    return v_flex().px(12).py(6)
      .child(div().pt(2).pb(2).font_medium().text_color(t.dim).child(L.other))
      .children(this.orderedSessions(sessions, activeSpace).map((s) => this.sessionRow(t, s)));
  }

  orderedSessions(list, activeSpace) {
    const active = [];
    const rest = [];
    for (const s of list) {
      const link = this.sessionLink[s.session_id];
      if (link && link.space === activeSpace) active.push(s);
      else rest.push(s);
    }
    const byAct = (a, b) => ((b.last_activity || 0) - (a.last_activity || 0));
    return active.sort(byAct).concat(rest.sort(byAct));
  }

  sessionRow(t, s) {
    const link = this.sessionLink[s.session_id];
    const hint = Number(this.cfg.hint_min_tokens);
    const min = Number.isFinite(hint) && hint > 0 ? hint : DEFAULTS.hint_min_tokens;
    const row = v_flex()
      .id("sess-" + s.pid)
      .py(4)
      .when(Boolean(link), (el) => el
        .cursor_pointer()
        .hover((h) => h.bg(t.hover))
        .on_click(() => run("agent.focus", { terminal: link.address })))
      .child(h_flex().gap(8).items_center()
        .child(div().flex_1().min_w(0).truncate().text_color(t.foreground).child(s.project || "?"))
        .child(div().flex_none().text_color(t.muted).child(fmtTokens(s.context_tokens)))
        .child(div().flex_none().text_color(t.dim).child(cacheLeft(s, this.view.now)))
        .child(div().flex_none().text_color(t.muted).child(usd(s.today_usd))));
    if (s.context_tokens != null && s.context_tokens >= min) {
      row.child(div().pt(1).text_size(12).text_color(t.muted).child(this.langTable().compact));
    }
    return row;
  }

  groupRows(t) {
    const groups = (Array.isArray(this.view.groups) ? this.view.groups : [])
      .filter((g) => g && typeof g === "object" && !Array.isArray(g));
    if (!groups.length) return [];
    const L = this.langTable();
    const head = h_flex().px(12).pb(2).gap(8)
      .child(div().flex_1().min_w(0).text_size(11).font_medium().text_color(t.dim).child(L.groups))
      .child(div().flex_none().w(60).text_right().text_size(11).text_color(t.dim).child(L.today_hdr))
      .child(div().flex_none().w(60).text_right().text_size(11).text_color(t.dim).child("5 h"))
      .child(div().flex_none().w(60).text_right().text_size(11).text_color(t.dim).child(L.week_hdr));
    const rows = groups.map((g) => h_flex().px(12).py(2).gap(8).items_center()
      .child(div().flex_1().min_w(0).truncate().text_color(t.foreground).child(g.label || g.id || "?"))
      .child(div().flex_none().w(60).text_right().text_color(t.muted).child(usd(g.today_usd)))
      .child(div().flex_none().w(60).text_right().text_color(t.muted).child(usd(g.five_hour_usd)))
      .child(div().flex_none().w(60).text_right().text_color(t.muted).child(usd(g.week_usd))));
    return [head, ...rows];
  }

  alertRows(t) {
    const newest = this.events.slice(-5).reverse();
    if (!newest.length) return [];
    return [
      div().px(12).pt(10).pb(3).text_size(11).font_medium().text_color(t.dim).child(this.langTable().recent),
      ...newest.map((e) => h_flex()
        .id("ev-" + e.id)
        .px(12)
        .py(2)
        .gap(8)
        .items_center()
        .child(div().flex_1().min_w(0).truncate().text_color(t.muted).child(e.title || ""))
        .child(div().flex_none().text_color(t.dim).child(hhmm(e.ts)))),
    ];
  }
}
