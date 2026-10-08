#!/usr/bin/env python3
"""Tests for tab visibility (the "Tabs" menu) in index.html.

Static checks keep the tab list honest; the behaviour checks extract the real
functions from index.html and run them under node against a small DOM stub, so
a render-time error that `node --check` cannot see still fails here.

Run: python3 scripts/test_tabs.py     (needs node for the behaviour checks)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = open(os.path.join(HERE, "..", "index.html"), encoding="utf-8").read()
FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def func_source(name):
    """Source of `function name(...) {...}` by brace matching."""
    m = re.search(r"^function " + re.escape(name) + r"\(", HTML, re.M)
    assert m, f"function {name} not found"
    i = HTML.index("{", m.end())
    depth, j = 0, i
    while True:
        c = HTML[j]
        depth += c == "{"
        depth -= c == "}"
        j += 1
        if depth == 0:
            return HTML[m.start():j]


def const_source(name):
    m = re.search(r"^const " + re.escape(name) + r" = .*?;\s*$", HTML, re.M | re.S)
    assert m, f"const {name} not found"
    end = HTML.index(";\n", m.start()) + 1
    return HTML[m.start():end]


nav = re.search(r'<nav id="tab-nav">(.*?)</nav>', HTML, re.S).group(1)
TABS = re.findall(r'data-tab="([a-z]+)"', nav)
switch = func_source("switchTab")

# ── static: every tab is fully wired ─────────────────────────────────────────
check("nav has tabs", len(TABS) >= 10, str(TABS))
titles = re.search(r"const titles = \{(.*?)\};", switch, re.S).group(1)
for t in TABS:
    check(f"switchTab shows a page for '{t}'", re.search(rf"tab === '{t}'\)", switch) is not None)
    check(f"title entry for '{t}'", re.search(rf"\b{t}:", titles) is not None)

check("VALID_TABS is derived from the nav, not a hand list",
      re.search(r"const VALID_TABS = \['dashboard', \.\.\.Array\.from\(document\.querySelectorAll\('#tab-nav", HTML) is not None)
check("log and finances are routable (they were missing from the old hand list)",
      "log" in TABS and "finances" in TABS)

always = json.loads(re.search(r"const ALWAYS_VISIBLE_TABS = (\[.*?\]);", HTML).group(1).replace("'", '"'))
default_hidden = json.loads(re.search(r"const DEFAULT_HIDDEN_TABS = (\[.*?\]);", HTML).group(1).replace("'", '"'))
check("services cannot be hidden", "services" in always and "dashboard" in always)
check("default-hidden are real tabs", all(t in TABS for t in default_hidden), str(default_hidden))
check("a fresh install keeps bookmarks, news, feeds, calendar, services",
      not set(default_hidden) & {"bookmarks", "news", "feeds", "calendar", "services"})
check("fresh install seeds hiddenTabs", "settings: { hiddenTabs: DEFAULT_HIDDEN_TABS.slice() }" in HTML)

# ── static: startup order (a TDZ error here blanks every tab and passes node --check) ──
load_call = [m.start() for m in re.finditer(r"^load\(\);", HTML, re.M)]
first_switch = HTML.index("switchTab(initialTab);")
for const in ("ALWAYS_VISIBLE_TABS", "DEFAULT_HIDDEN_TABS", "TAB_FRAMES", "TAB_FRAME_URLS"):
    pos = HTML.index(f"const {const} ")
    check(f"{const} is declared before load() and switchTab(initialTab)",
          bool(load_call) and pos < load_call[0] and pos < first_switch)
check("applyTabVisibility is called from load() and afterStateSwap()",
      "applyTabVisibility();" in func_source("load") and "applyTabVisibility();" in func_source("afterStateSwap"))

# ── behaviour (node + DOM stub) ──────────────────────────────────────────────
HARNESS = r"""
const TABS = %(tabs)s;
const els = {};
function mkBtn(t) { return { dataset: { tab: t }, style: {}, textContent: '#' + t + ' Label' }; }
const btns = TABS.map(mkBtn);
const frames = { 'leafwiki-frame': {attrs:{}, set src(v){this.attrs.src=v}, getAttribute(k){return this.attrs[k]||null}},
                 'dailytxt-frame': {attrs:{}, set src(v){this.attrs.src=v}, getAttribute(k){return this.attrs[k]||null}},
                 'freshrss-frame': {attrs:{}, set src(v){this.attrs.src=v}, getAttribute(k){return this.attrs[k]||null}} };
const menu = { innerHTML: '' };
const document = {
  querySelectorAll: (sel) => sel.includes('#tab-nav') ? btns : [],
  getElementById: (id) => id === 'tab-menu' ? menu : (frames[id] || null),
};
function escHtml(s) { return String(s ? s : '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
let saves = 0; function save() { saves++; }
let switched = [];
function switchTab(t) { switched.push(t); currentTab = t; }
let currentTab = 'dashboard';
let state = { settings: {} };
%(consts)s
%(funcs)s
const out = {};
const vis = () => Object.fromEntries(btns.map(b => [b.dataset.tab, b.style.display !== 'none']));
const assert = (name, ok) => { out[name] = !!ok; };

// 1. nothing hidden by default for an existing install (no hiddenTabs key)
applyTabVisibility();
assert('existing install shows every tab', Object.values(vis()).every(Boolean));

// 2. hide / show
setTabHidden('finances', true);
assert('hide finances', vis().finances === false && state.settings.hiddenTabs.includes('finances') && saves === 1);
setTabHidden('finances', true);
assert('hiding twice does not duplicate', state.settings.hiddenTabs.filter(t => t === 'finances').length === 1);
setTabHidden('finances', false);
assert('show finances again', vis().finances === true && !state.settings.hiddenTabs.includes('finances'));

// 3. dashboard and services cannot be hidden
setTabHidden('services', true); setTabHidden('dashboard', true);
assert('services stays visible', vis().services === true && !hiddenTabs().includes('services'));
assert('menu never lists services', !menu.innerHTML.includes('data-tab-toggle="services"'));

// 4. hiding the current tab falls back to the dashboard
currentTab = 'music'; switched = [];
setTabHidden('music', true);
assert('hiding the current tab goes to dashboard', switched[switched.length-1] === 'dashboard');

// 5. a remote change hides the tab we are on
currentTab = 'news'; switched = [];
state = { settings: { hiddenTabs: ['news'] } };
applyTabVisibility();
assert('remote hide of current tab goes to dashboard', switched.includes('dashboard') && vis().news === false);

// 6. unknown ids are harmless; garbage settings are tolerated
state = { settings: { hiddenTabs: ['nope', 'journal'] } };
applyTabVisibility();
assert('unknown id ignored', vis().journal === false && Object.keys(vis()).every(t => TABS.includes(t)));
state = { settings: { hiddenTabs: 'oops' } }; applyTabVisibility();
assert('non-array hiddenTabs treated as none', Object.values(vis()).every(Boolean));
state = {}; applyTabVisibility();
assert('missing settings tolerated', Object.values(vis()).every(Boolean));

// 7. switchTab() refuses hidden tabs (the real guard line)
state = { settings: { hiddenTabs: ['finances'] } };
const realGuard = %(guard)s;
assert('router guard redirects hidden tab', realGuard);

// 8. iframes: hidden tab's frame is not loaded; unhiding loads it
for (const f of Object.values(frames)) f.attrs = {};
TAB_FRAME_URLS.leafwiki = 'https://wiki'; TAB_FRAME_URLS.journal = 'https://j'; TAB_FRAME_URLS.reader = 'https://r';
state = { settings: { hiddenTabs: ['leafwiki', 'journal', 'reader'] } };
Object.keys(TAB_FRAMES).forEach(loadTabFrame);
assert('hidden tabs do not load iframes', Object.values(frames).every(f => !f.getAttribute('src')));
setTabHidden('reader', false);
assert('unhiding loads only that iframe', frames['freshrss-frame'].getAttribute('src') === 'https://r' && !frames['leafwiki-frame'].getAttribute('src'));

// 9. menu content
state = { settings: { hiddenTabs: ['log'] } }; renderTabMenu();
assert('menu has one checkbox per toggleable tab', (menu.innerHTML.match(/type="checkbox"/g) || []).length === TABS.length - 1);
assert('unchecked when hidden', /data-tab-toggle="log"><\/label>|data-tab-toggle="log"> <span>/.test(menu.innerHTML) && !/data-tab-toggle="log" checked/.test(menu.innerHTML));
assert('checked when visible', /data-tab-toggle="news" checked/.test(menu.innerHTML));
assert('has Show all', menu.innerHTML.includes('btn-tabs-all'));
console.log(JSON.stringify(out));
"""

if not shutil.which("node"):
    print("SKIP behaviour checks: node not found")
else:
    funcs = "\n".join(func_source(n) for n in
                      ("hiddenTabs", "isTabVisible", "loadTabFrame", "applyTabVisibility", "setTabHidden", "renderTabMenu"))
    consts = "\n".join(const_source(n) for n in ("ALWAYS_VISIBLE_TABS", "DEFAULT_HIDDEN_TABS", "TAB_FRAMES", "TAB_FRAME_URLS"))
    # The guard line in switchTab, evaluated for a hidden tab.
    guard_line = "if (!isTabVisible(tab)) tab = 'dashboard';"
    check("switchTab has the hidden-tab guard as its first statement",
          switch.split("{", 1)[1].lstrip().startswith("if (!isTabVisible(tab)) tab = 'dashboard';"))
    guard_expr = "(function(){ let tab = 'finances'; " + guard_line + " return tab === 'dashboard'; })()"
    js = HARNESS % {"tabs": json.dumps(TABS), "funcs": funcs, "consts": consts, "guard": guard_expr}
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
        path = f.name
    r = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
    os.unlink(path)
    if r.returncode != 0:
        check("harness runs", False, r.stderr.strip()[-400:])
    else:
        for name, ok in json.loads(r.stdout.strip().splitlines()[-1]).items():
            check(name, ok)

print()
print(f"{len(FAILS)} failure(s)" if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
