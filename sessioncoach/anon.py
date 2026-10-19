"""Anonymous mode: consistent pseudonyms for projects, tenants, accounts and budgets.

For screenshots and demos. Names are replaced by "Project A", "Workspace 1", "Account 1" ...
ordered by all-time spend, so the same entity keeps the same pseudonym across every view, and
filters keep working (incoming pseudonyms are mapped back).
"""
import collections
import re
import string

from .analysis import Dims

LABELS = {"es": ("Proyecto", "Empresa", "Cuenta", "Presupuesto"), "en": ("Project", "Workspace", "Account", "Budget")}


def _letters(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = string.ascii_uppercase[r] + s
    return s


class Anonymizer:
    def __init__(self, con, budgets=(), lang="es", labeler=None, resolver=None):
        p_lab, t_lab, a_lab, b_lab = LABELS.get(lang, LABELS["en"])
        self._labels = {"project": p_lab, "budget": b_lab}
        self._extra = {"project": 0, "budget": 0}
        dims = Dims(con, labeler, resolver)
        proj, ten, acc = collections.Counter(), collections.Counter(), collections.Counter()
        acc_names, dir_to_proj = {}, {}
        for s in con.execute("SELECT project_dir, root, SUM(usd) AS usd FROM cost_slices GROUP BY project_dir, root"):
            p, t, a = dims(s)
            proj[p] += s["usd"]
            ten[t] += s["usd"]
            acc[a["id"]] += s["usd"]
            acc_names[a["id"]] = (a["label"], a["company"])
            dir_to_proj[s["project_dir"]] = p
        self.map = {}
        for i, (p, _) in enumerate(proj.most_common()):
            self.map[p] = "%s %s" % (p_lab, _letters(i))
        for i, (t, _) in enumerate(ten.most_common()):
            self.map.setdefault(t, "%s %d" % (t_lab, i + 1))
        acc_pseudo = {a: "%s %d" % (a_lab, i + 1) for i, (a, _) in enumerate(acc.most_common())}
        for a, v in acc_pseudo.items():  # an account id may equal a tenant name; the tenant keeps its pseudonym
            self.map.setdefault(a, v)
        for i, b in enumerate(budgets):
            if b.get("name"):
                self.map.setdefault(b["name"], "%s %d" % (b_lab, i + 1))
        # reverse map (for filters) only from entity names, never from aliases below
        self.reverse = {}
        for k, v in self.map.items():
            self.reverse.setdefault(v, k)
        for a, (label, _) in acc_names.items():
            self.map[label] = acc_pseudo[a]
        for d, p in dir_to_proj.items():
            self.map.setdefault(d, self.map[p])
        # names that may appear inside other strings (e.g. "<project>-verifier" agent names)
        parts = sorted({n for n in list(proj) + list(ten) if n and len(n) >= 3}, key=len, reverse=True)
        self._inner = re.compile(r"(?<![A-Za-z0-9])(%s)(?![A-Za-z0-9])" % "|".join(re.escape(n) for n in parts),
                                 re.I) if parts else None
        self._slug = {n.lower(): self.map[n].lower().replace(" ", "-") for n in parts}

    def cover(self, value, kind="project"):
        """`value` with a pseudonym even when it was never seen with spend (a project not ingested yet, a budget since
        renamed or removed): those get a new one ("Project ?1", "Budget ?1"), consistent within this view."""
        if not value or value in self.map or value in self.reverse:
            return value
        self._extra[kind] += 1
        self.map[value] = "%s ?%d" % (self._labels[kind], self._extra[kind])
        return value

    def unmap(self, value):
        return self.reverse.get(value, value) if value else value

    def apply(self, obj):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if k == "db":
                    out[k] = "<data dir>/metrics.db"
                    continue
                out[self._s(k) if isinstance(k, str) else k] = self.apply(v)
            return out
        if isinstance(obj, list):
            return [self.apply(x) for x in obj]
        if isinstance(obj, str):
            return self._s(obj)
        return obj

    def _s(self, s):
        if s in self.map:
            return self.map[s]
        if " · " in s:  # e.g. "project · session"
            return " · ".join(self._s(part) for part in s.split(" · "))
        if self._inner is not None:
            return self._inner.sub(lambda m: self._slug.get(m.group(1).lower(), m.group(1)), s)
        return s
