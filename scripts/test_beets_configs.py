#!/usr/bin/env python3
"""beets-manual.yaml must name files exactly like beets-inbox.yaml.

The manual config is a standalone copy (beets ignores `include:` in a -c file),
so nothing but this test stops the two drifting apart.
"""
import os
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
load = lambda n: yaml.safe_load(open(os.path.join(HERE, n)))
inbox, manual = load("beets-inbox.yaml"), load("beets-manual.yaml")

fails = 0
for key in ("paths", "replace", "plugins"):
    ok = inbox.get(key) == manual.get(key)
    print(("  ok   " if ok else "  FAIL ") + f"{key} identical in both configs")
    fails += not ok

# The manual config must be interactive and non-destructive.
imp = manual.get("import", {})
for k, want in (("quiet", False), ("copy", True), ("move", False)):
    ok = imp.get(k) == want
    print(("  ok   " if ok else "  FAIL ") + f"manual import.{k} == {want}")
    fails += not ok

# And beets must actually be loading the keys (not silently ignoring them).
out = subprocess.run(["beet", "-c", os.path.join(HERE, "beets-manual.yaml"), "config"],
                     capture_output=True, text=True).stdout
# beets prints the curly quotes as \u2019 escapes, not literally.
ok = "Singles/$artist - $title" in out and "\\u2019" in out
print(("  ok   " if ok else "  FAIL ") + "beets resolves the paths/replace blocks")
fails += not ok

print(f"\n{fails} failed")
sys.exit(1 if fails else 0)
