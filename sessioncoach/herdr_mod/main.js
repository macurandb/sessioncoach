// A Herdr panel for sessioncoach: the decision view over the running Claude sessions — am I OK
// this week, and what should I do right now — from `sessioncoach live --json`. Read-only except for
// the group sync it feeds back.
//
// It reads: `sessioncoach live --json` (the running sessions, limits, accounts, events), its own
// `config.json`, the shared keys `space-groups` (from herdr-space-groups), `sessioncoach.toasted`
// and `sessioncoach.folded`, and Herdr's `spaces()`/`agents()`/`hc get` for linking.
// It writes: one staging file `<app home>/mods/sessioncoach/.groups-sync.json`, runs
// `sessioncoach group sync-herdr --json <file>` (which edits `projects.json`), and keeps its state
// in the shared keys `sessioncoach.toasted` and `sessioncoach.folded`. Nothing else.

import { View, div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import {
  exec, hc, readTextFile, writeTextFile, appHome, checking, theme, spaces, agents, run,
  shared, setShared, notify,
} from "herdr";
import {
  buildSyncPayload, buildActions, weekVerdict, fiveHourWarn, fiveHourWarnText,
  fmtPct, hhmm, fmtTokens, cacheLeft, usd, usdInt, ppcUsable,
} from "./logic.js";

const DEFAULTS = {
  command: "sessioncoach", refresh_s: 30, toasts: true, hint_min_tokens: 300000,
  sync_herdr_groups: true,
};
const TOASTED = "sessioncoach.toasted";
const FOLDED = "sessioncoach.folded";
const SYNC_FILE = "/mods/sessioncoach/.groups-sync.json";

// The panel's own section labels and error lines, in the language `live --json` reports (`lang`).
const LANG = {
  es: {
    now: "AHORA", nothing: "Nada urgente", clients: "Clientes", sessions: "Sesiones",
    alerts: "Alertas", this_week: "esta semana", today: "hoy",
    none: "No hay ninguna sesión de Claude Code en marcha",
    notFound: "sessioncoach no encontrado: vuelve a ejecutar `sessioncoach herdr-install` (comando: ",
    liveFailed: "sessioncoach live falló (exit ",
    invalidJson: "sessioncoach live: JSON inválido",
    unknownVersion: "sessioncoach live: versión desconocida ",
    syncFailed: "group sync falló (exit ",
    syncErr: "group sync: ",
    syncGroups: "group sync: demasiados grupos",
    syncFolders: "group sync: demasiadas carpetas en un grupo",
    syncName: "group sync: nombre de grupo demasiado largo",
    syncPath: "group sync: ruta de carpeta demasiado larga",
  },
  en: {
    now: "NOW", nothing: "Nothing urgent", clients: "Clients", sessions: "Sessions",
    alerts: "Alerts", this_week: "this week", today: "today",
    none: "No Claude Code session running",
    notFound: "sessioncoach not found: run `sessioncoach herdr-install` again (command: ",
    liveFailed: "sessioncoach live failed (exit ",
    invalidJson: "sessioncoach live: invalid JSON",
    unknownVersion: "sessioncoach live: unknown version ",
    syncFailed: "group sync failed (exit ",
    syncErr: "group sync: ",
    syncGroups: "group sync: too many groups",
    syncFolders: "group sync: too many folders in a group",
    syncName: "group sync: group name too long",
    syncPath: "group sync: folder path too long",
  },
};

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
    this.sessionLink = {};        // pid -> { address, space } (a pid is unique)
    this.sessionLinkBySid = new Map();  // session_id -> [{address, space}, ...] for toast linking
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

  lang() {
    return this.view && this.view.lang === "es" ? "es" : "en";
  }

  langTable() {
    return LANG[this.lang()];
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
      this.error = this.langTable().notFound + command + ")";
      return null;
    }
    if (result.code !== 0) {
      this.error = this.langTable().liveFailed + result.code + ")";
      return null;
    }
    let data;
    try {
      data = JSON.parse(result.stdout);
    } catch (e) {
      this.error = this.langTable().invalidJson;
      return null;
    }
    if (!data || data.version !== 1) {
      this.error = this.langTable().unknownVersion + (data && data.version);
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
      if (payload == null) return;  // no usable shared state, or corrupt input: nothing to do
      if (payload.error) {  // valid but over sync-herdr's limits: no sync, say why
        this.syncError = this.syncLimitText(payload.error);
        return;
      }
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
        this.syncError = this.langTable().syncFailed + result.code + ")";
      }
    } catch (e) {
      this.syncError = this.langTable().syncErr + (e && e.message ? e.message : String(e));
    }
  }

  syncLimitText(code) {
    const L = this.langTable();
    const map = { groups: L.syncGroups, folders: L.syncFolders, name: L.syncName, path: L.syncPath };
    return map[code] || (L.syncErr + code);
  }

  // Write the payload, passing the staging file's current text as `expected`. `writeTextFile(path,
  // text, expected)` writes only while the file still matches `expected`, and `expected: null` means
  // "the file is absent — create it" (see gpui-kit.d.ts). `readTextFile` returns null for an absent
  // file and rejects for one that exists but cannot be read, so an unreadable staging file is never
  // overwritten blindly: the reject propagates to syncGroups and the error line is shown instead.
  async stagePayload(path, text) {
    let expected;
    try {
      expected = await readTextFile(path);
    } catch (e) {
      throw e;  // unreadable: do not write expected=null over a file we could not read
    }
    try {
      await writeTextFile(path, text, expected);
    } catch (e) {
      expected = await readTextFile(path);  // a stale snapshot: re-read once and retry
      await writeTextFile(path, text, expected);
    }
  }

  // Match each session to a terminal: pid first, then cwd only when exactly one
  // agent has it (two Claude tabs in one repo are common). Links are keyed by pid (unique); a
  // separate session_id -> links map lets a toast link only when its id names exactly one session.
  linkSessions(data) {
    const links = {};
    const bySid = new Map();
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
      if (!address || s.pid == null) continue;
      const link = { address, space: this.addrSpace.get(address) };
      links[s.pid] = link;
      if (s.session_id != null) {
        if (!bySid.has(s.session_id)) bySid.set(s.session_id, []);
        bySid.get(s.session_id).push(link);
      }
    }
    this.sessionLink = links;
    this.sessionLinkBySid = bySid;
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
      const matches = e.session_id != null ? this.sessionLinkBySid.get(e.session_id) : undefined;
      const link = matches && matches.length === 1 ? matches[0] : undefined;
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
      children.push(...this.verdictBlocks(t));
      children.push(...this.nowBlock(t));
      children.push(...this.clientsBlock(t));
      children.push(...this.sessionsBlock(t));
      children.push(...this.alertsBlock(t));
      return root.children(children);
    } catch (e) {  // never throw out of render
      return v_flex().size_full().bg(theme().chrome).p(10).child(div().text_color(theme().dim).child("sessioncoach"));
    }
  }

  okColor(t) {
    return t.done != null ? t.done : t.foreground;
  }

  // ---- A. the verdict, one block per account ----

  verdictBlocks(t) {
    const accounts = (Array.isArray(this.view.accounts) ? this.view.accounts : [])
      .filter((a) => a && typeof a === "object" && !Array.isArray(a));
    if (!accounts.length) return [];
    const lang = this.lang();
    const multi = accounts.length >= 2;
    const out = [];
    for (const a of accounts) {
      const block = v_flex().px(12).py(6);
      if (multi) {
        block.child(div().pb(2).text_size(11).font_medium().text_color(t.dim).child(a.label || a.key || "?"));
      }
      if (a.seven_day && a.seven_day.pct != null) block.child(this.weekRow(t, a.seven_day, lang));
      if (a.five_hour && a.five_hour.pct != null) block.child(this.fiveRow(t, a.five_hour, lang));
      const dollars = this.dollarsLine(a, lang, this.langTable());
      if (dollars) block.child(div().pt(1).text_size(12).text_color(t.muted).child(dollars));
      out.push(block);
    }
    return out;
  }

  weekRow(t, seven, lang) {
    const v = weekVerdict(seven, lang);
    const color = v.state === "red" ? t.blocked : v.state === "amber" ? t.working
      : v.state === "green" ? this.okColor(t) : t.foreground;
    const parts = [div().flex_none().text_color(t.foreground).child(v.base)];
    if (v.tail) parts.push(div().flex_1().min_w(0).text_color(color).child(" · " + v.tail));
    return h_flex().gap(0).items_center().children(parts);
  }

  fiveRow(t, five, lang) {
    const es = lang === "es";
    const parts = [div().flex_none().text_color(t.foreground).child("5 h " + fmtPct(five.pct, lang))];
    if (fiveHourWarn(five)) {
      parts.push(div().flex_1().min_w(0).text_color(t.blocked).child(" · " + fiveHourWarnText(five, lang)));
    }
    if (five.resets_at != null) {
      parts.push(div().flex_1().min_w(0).text_color(t.dim)
        .child(" · " + (es ? "se reinicia " : "resets ") + hhmm(five.resets_at)));
    }
    return h_flex().gap(0).items_center().children(parts);
  }

  dollarsLine(a, lang, L) {
    const seven = a.seven_day || {};
    const parts = [];
    if (seven.window_usd != null) parts.push(usdInt(seven.window_usd, lang) + " " + L.this_week);
    if (a.today_usd != null) parts.push(usdInt(a.today_usd, lang) + " " + L.today);
    return parts.length ? parts.join(" · ") : "";
  }

  // ---- B. NOW, the actions ----

  nowBlock(t) {
    const hint = Number(this.cfg.hint_min_tokens);
    const min = Number.isFinite(hint) && hint > 0 ? hint : DEFAULTS.hint_min_tokens;
    const actions = buildActions(this.view, min);
    const L = this.langTable();
    const rows = [div().px(12).pt(8).pb(2).text_size(11).font_medium().text_color(t.dim).child(L.now)];
    if (!actions.length) {
      rows.push(div().px(12).pb(4).text_size(12).text_color(t.dim).child(L.nothing));
      return rows;
    }
    for (const act of actions) {
      const link = this.sessionLink[act.session.pid];
      const color = act.kind === "resume" ? t.working : t.foreground;
      const row = h_flex()
        .id("act-" + act.session.pid)
        .px(12)
        .py(2)
        .gap(8)
        .items_center()
        .when(Boolean(link), (el) => el
          .cursor_pointer()
          .hover((h) => h.bg(t.hover))
          .on_click(() => run("agent.focus", { terminal: link.address })))
        .child(div().flex_1().min_w(0).text_color(color).child(act.line));
      rows.push(row);
    }
    return rows;
  }

  // ---- C. Clients, folded by default ----

  clientsBlock(t) {
    const accounts = (Array.isArray(this.view.accounts) ? this.view.accounts : [])
      .filter((a) => a && typeof a === "object" && !Array.isArray(a));
    // the current login: the account with the most sessions; its groups must add up to its window
    const acct = accounts.reduce((best, a) => (!best || (a.sessions || 0) > (best.sessions || 0)) ? a : best, null);
    if (!acct || !Array.isArray(acct.groups) || !acct.groups.length) return [];
    const L = this.langTable();
    const lang = this.lang();
    const labels = {};
    for (const g of Array.isArray(this.view.groups) ? this.view.groups : []) {
      if (g && g.id != null) labels[String(g.id)] = g.label || g.id;
    }
    const key = "clients";
    const rows = this.sectionHeader(t, key, L.clients);
    if (this.isFolded(key)) return rows;
    const weekPpc = acct.seven_day && acct.seven_day.usd_per_pct;
    const fivePpc = acct.five_hour && acct.five_hour.usd_per_pct;
    const weekOk = ppcUsable(weekPpc);
    const fiveOk = ppcUsable(fivePpc);
    for (const g of acct.groups) {
      if (!g || typeof g !== "object") continue;
      const label = labels[String(g.id)] || g.id || "?";
      const week = weekOk ? fmtPct((g.seven_day_usd || 0) / weekPpc, lang) : usd(g.seven_day_usd, lang);
      const five = fiveOk ? fmtPct((g.five_hour_usd || 0) / fivePpc, lang) : usd(g.five_hour_usd, lang);
      rows.push(h_flex().px(12).py(2).gap(8).items_center()
        .child(div().flex_1().min_w(0).truncate().text_color(t.foreground).child(label))
        .child(div().flex_none().text_color(t.dim).child("·"))
        .child(div().flex_none().text_color(t.muted).child(week))
        .child(div().flex_none().text_color(t.dim).child("·"))
        .child(div().flex_none().text_color(t.muted).child(five)));
    }
    return rows;
  }

  // ---- D. Sessions, folded ----

  sessionsBlock(t) {
    const sessions = Array.isArray(this.view.sessions) ? this.view.sessions : [];
    const L = this.langTable();
    const key = "sessions";
    const rows = this.sectionHeader(t, key, L.sessions + " (" + sessions.length + ")");
    if (this.isFolded(key)) return rows;
    const activeSpace = (spaces().find((s) => s.active) || {}).id;
    const active = [];
    const rest = [];
    for (const s of sessions) {
      if (!s || typeof s !== "object") continue;
      const link = this.sessionLink[s.pid];
      if (link && link.space === activeSpace) active.push(s);
      else rest.push(s);
    }
    const byAct = (a, b) => ((b.last_activity || 0) - (a.last_activity || 0));
    const ordered = active.sort(byAct).concat(rest.sort(byAct));
    if (!ordered.length) {
      rows.push(div().px(12).pt(4).pb(6).text_size(12).text_color(t.dim).child(L.none));
    }
    for (const s of ordered) rows.push(this.sessionRow(t, s));
    return rows;
  }

  sessionRow(t, s) {
    const lang = this.lang();
    const link = this.sessionLink[s.pid];
    return v_flex()
      .id("sess-" + s.pid)
      .px(12)
      .py(4)
      .when(Boolean(link), (el) => el
        .cursor_pointer()
        .hover((h) => h.bg(t.hover))
        .on_click(() => run("agent.focus", { terminal: link.address })))
      .child(h_flex().gap(8).items_center()
        .child(div().flex_1().min_w(0).truncate().text_color(t.foreground).child(s.project || "?"))
        .child(div().flex_none().text_color(t.muted).child(fmtTokens(s.context_tokens, lang)))
        .child(div().flex_none().text_color(t.dim).child(cacheLeft(s, this.view.now, lang)))
        .child(div().flex_none().text_color(t.muted).child(usd(s.today_usd, lang))));
  }

  // ---- E. Alerts, folded ----

  alertsBlock(t) {
    const newest = this.events.slice(-5).reverse();
    if (!newest.length) return [];
    const L = this.langTable();
    const key = "alerts";
    const rows = this.sectionHeader(t, key, L.alerts);
    if (this.isFolded(key)) return rows;
    for (const e of newest) {
      rows.push(h_flex()
        .id("ev-" + e.id)
        .px(12)
        .py(2)
        .gap(8)
        .items_center()
        .child(div().flex_1().min_w(0).truncate().text_color(t.muted).child(e.title || ""))
        .child(div().flex_none().text_color(t.dim).child(hhmm(e.ts))));
    }
    return rows;
  }

  // ---- fold state, one shared key ----

  foldState() {
    const f = shared(FOLDED);
    return f && typeof f === "object" && !Array.isArray(f) ? f : {};
  }

  isFolded(key) {
    const folded = this.foldState();
    return typeof folded[key] === "boolean" ? folded[key] : true;  // sections start folded
  }

  toggleFold(key, cx) {
    const folded = this.foldState();
    folded[key] = !this.isFolded(key);
    setShared(FOLDED, { ...folded });
    cx.notify();
  }

  sectionHeader(t, key, title) {
    const folded = this.isFolded(key);
    return [h_flex()
      .id("section-" + key)
      .px(12).pt(8).pb(2).gap(6).items_center().cursor_pointer()
      .on_click((_e, cx) => this.toggleFold(key, cx))
      .child(div().flex_1().min_w(0).text_size(11).font_medium().text_color(t.dim).child(title))
      .child(div().flex_none().text_color(t.dim).child(folded ? "›" : "˅"))];
  }
}
