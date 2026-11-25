"""The panel's pure logic (sessioncoach/herdr_mod/logic.js) is checked with node.

The harness (tests/js/test_logic.mjs) copies logic.js to a temporary .mjs — so the `export`
statements are ESM whatever the node version — imports it and asserts the verdict states, action
ordering and limit, the es/en strings, the decimals and the null-usd_per_pct fallback. Skipped
cleanly when node is missing (the CI runners have it).
"""
import os
import shutil
import subprocess
import unittest

import isolate  # noqa: F401  (drops SESSIONCOACH_* left in the shell)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, "tests", "js", "test_logic.mjs")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class LogicJs(unittest.TestCase):
    def test_logic_js_harness_passes(self):
        result = subprocess.run([shutil.which("node"), HARNESS], capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("logic.js ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
