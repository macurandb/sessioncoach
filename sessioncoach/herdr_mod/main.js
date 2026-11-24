// A Herdr panel for sessioncoach: the running Claude sessions and what they need
// right now, from `sessioncoach live --json`. Read-only: it runs that one command,
// reads one config file and writes one shared key; nothing else.

import { View, div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import {
  exec, hc, readTextFile, appHome, checking, theme, spaces, agents, run,
  shared, setShared, notify,
} from "herdr";

const DEFAULTS = { command: "sessioncoach", refresh_s: 30, toasts: true, hint_min_tokens: 300000 };
const TOASTED = "sessioncoach.toasted";

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

function pctColor(t, pct) {
  if (pct >= 90) return t.blocked;
  if (pct >= 75) return t.working;
  return t.foreground;
}

export default class SessionCoach extends View {
  init(_props, cx) {
    this.cfg = { ...DEFAULTS };
    this.view = null;          // last good `live --json` document
    this.error = null;         // one-line error, shown above the last good data
    this.cursor = null;        // in memory only, never in shared
    this.firstPoll = true;     // the first poll after this start lists the backlog and toasts nothing
    this.events = [];          // events received, in memory, oldest first
    this.sessionLink = {};     // session_id -> { address, space }
    this.addrPid = new Map();  // agent address -> foreground pid
    this.addrCwd = new Map();  // agent address -> foreground cwd
    this.addrSpace = new Map();// agent address -> space id
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

  // Match each session to a terminal: pid first, then cwd only when exactly one
  // agent has it (two Claude tabs in one repo are common).
  linkSessions(data) {
    const links = {};
    for (const s of Array.isArray(data.sessions) ? data.sessions : []) {
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
      children.push(...this.limitsLine(t));
      children.push(...this.sessionRows(t));
      children.push(...this.alertRows(t));
      return root.children(children);
    } catch (e) {  // never throw out of render
      return v_flex().size_full().bg(theme().chrome).p(10).child(div().text_color(theme().dim).child("sessioncoach"));
    }
  }

  limitsLine(t) {
    const lim = this.view.limits;
    if (!lim) return [];
    const five = lim && lim.five_hour;
    const seven = lim && lim.seven_day;
    const group = (label, win) => {
      const parts = [];
      parts.push(div().flex_none().child(label + " "));
      parts.push(div().flex_none().text_color(pctColor(t, win.pct)).child(Math.round(win.pct) + "%"));
      if (label === "5h" && win.resets_at != null) parts.push(div().flex_none().text_color(t.dim).child(" ↻" + hhmm(win.resets_at)));
      return h_flex().flex_none().gap(2).items_center().children(parts);
    };
    const g = [];
    if (five && five.pct != null) g.push(group("5h", five));
    if (seven && seven.pct != null) g.push(group("7d", seven));
    if (!g.length) return [];
    const parts = [];
    g.forEach((gr, i) => {
      parts.push(gr);
      if (i < g.length - 1) parts.push(div().flex_none().px(6).text_color(t.dim).child("·"));
    });
    return [h_flex().px(12).pt(10).pb(4).gap(0).items_center().children(parts)];
  }

  sessionRows(t) {
    const sessions = Array.isArray(this.view.sessions) ? this.view.sessions : [];
    const activeSpace = (spaces().find((s) => s.active) || {}).id;
    const active = [];
    const rest = [];
    for (const s of sessions) {
      const link = this.sessionLink[s.session_id];
      if (link && link.space === activeSpace) active.push(s);
      else rest.push(s);
    }
    const byAct = (a, b) => ((b.last_activity || 0) - (a.last_activity || 0));
    const ordered = active.sort(byAct).concat(rest.sort(byAct));
    if (!ordered.length) {
      return [div().px(12).pt(10).pb(6).text_size(12).text_color(t.dim).child("No Claude Code session running")];
    }
    const hint = Number(this.cfg.hint_min_tokens);
    const min = Number.isFinite(hint) && hint > 0 ? hint : DEFAULTS.hint_min_tokens;
    return ordered.map((s) => {
      const link = this.sessionLink[s.session_id];
      const row = v_flex()
        .id("sess-" + s.pid)
        .px(12)
        .py(6)
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
        row.child(div().pt(1).text_size(12).text_color(t.muted).child("⚠ /compact or /handoff"));
      }
      return row;
    });
  }

  alertRows(t) {
    const newest = this.events.slice(-5).reverse();
    if (!newest.length) return [];
    return [
      div().px(12).pt(10).pb(3).text_size(11).font_medium().text_color(t.dim).child("Recent alerts"),
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
