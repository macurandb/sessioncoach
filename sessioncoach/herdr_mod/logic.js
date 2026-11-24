// Pure helpers for the sessioncoach panel. No imports from "herdr" or "gpui-kit": small, inert
// functions the mod calls, so their behaviour is easy to reason about and to reuse.

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

// Bucket sessions into per-account cards (by account.id) plus an "other" list: sessions whose
// account_id matches no card, or every session when there are no accounts. Bad rows are skipped.
export function groupSessionsByAccount(accounts, sessions) {
  const cards = [];
  const byId = new Map();
  for (const a of Array.isArray(accounts) ? accounts : []) {
    if (!a || typeof a !== "object" || Array.isArray(a) || typeof a.id !== "string" || !a.id) continue;
    const card = { account: a, sessions: [] };
    cards.push(card);
    byId.set(a.id, card);
  }
  const other = [];
  for (const s of Array.isArray(sessions) ? sessions : []) {
    if (!s || typeof s !== "object" || Array.isArray(s)) continue;
    const card = typeof s.account_id === "string" ? byId.get(s.account_id) : undefined;
    if (card) card.sessions.push(s);
    else other.push(s);
  }
  return { cards, other };
}
