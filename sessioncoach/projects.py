"""Human labels for Claude Code project dirs ('-Users-me-code-app' -> 'code/app').

An optional private file (<data dir>/projects.json) maps dirs to a project and a tenant
(workspace), e.g. to merge git worktrees into one project:

  {"rules": [{"match": "-Users-me-code-app*", "project": "app", "tenant": "acme"}]}

Rules are fnmatch patterns over the dir name; the first match wins.
"""
import fnmatch

from . import paths
from .collect import encode_dir

UNASSIGNED = "unassigned"


class Labeler:
    def __init__(self, rules=None):
        self.rules = rules or []
        self._cache = {}
        home = encode_dir(paths.HOME)  # Claude Code: every non-alphanumeric char becomes "-" (C:\Users\me -> C--Users-me)
        self._prefix = home + "-"

    @classmethod
    def load(cls, path=None):
        path = path or paths.projects_map_path()
        rules = paths.load_json_object(path).get("rules")
        return cls([r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else [])

    def __call__(self, project_dir):
        """-> (project, tenant)"""
        if project_dir not in self._cache:
            self._cache[project_dir] = self._resolve(project_dir)
        return self._cache[project_dir]

    def _resolve(self, project_dir):
        for r in self.rules:
            if fnmatch.fnmatch(project_dir, r.get("match", "")):
                # a rule without "project" keeps the automatic label and only sets the tenant
                return r.get("project") or self._default(project_dir), r.get("tenant") or UNASSIGNED
        return self._default(project_dir), UNASSIGNED

    def _default(self, project_dir):
        name = project_dir[len(self._prefix):] if project_dir.startswith(self._prefix) else project_dir.lstrip("-")
        return name.replace("-", "/") or project_dir
