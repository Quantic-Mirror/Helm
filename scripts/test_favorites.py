#!/usr/bin/env python3
"""Tests for the Favorites page logic in index.html (status matching, lyrics).

Extracts the real functions from index.html and runs them under node, so a
rename or a regression fails here. Needs node.   Run: python3 scripts/test_favorites.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HTML = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "index.html"), encoding="utf-8").read()


def block(start_pat, kind):
    m = re.search(start_pat, HTML, re.M)
    assert m, start_pat
    if kind == "func":
        i = HTML.index("{", m.end())
        depth, j = 0, i
        while True:
            depth += HTML[j] == "{"
            depth -= HTML[j] == "}"
            j += 1
            if depth == 0:
                return HTML[m.start():j]
    return HTML[m.start():HTML.index(";\n", m.start()) + 1]


FUNCS = [r"^(?:async )?function musicxNorm\(", r"^function musicxStatusFor\(", r"^function musicxCleanTitle\(",
         r"^function parseLrc\(", r"^function musicxLineIndex\(", r"^async function fetchMusicxLyrics\("]
code = block(r"^const MUSICX_STATUS_RANK", "const") + "\n" + "\n".join(block(p, "func") for p in FUNCS)

JS = r"""
const MUSICX_LYRICS = {};
%(code)s
const out = {};
const ok = (n, v) => { out[n] = !!v; };

ok('norm ignores case/punctuation', musicxNorm("  Hard-To Love! ") === 'hardtolove');
const E = [{artist:'Snail Mail',title:'Cruise',status:'queued'},{artist:'snail mail',title:'CRUISE',status:'synced'},
           {artist:'Mini Trees',title:'Hard To Love',status:'failed'},{query:'x',status:'synced'},{artist:'A',title:'B',status:'weird'}];
ok('best status wins', musicxStatusFor({artist:'Snail Mail',song:'Cruise'}, E) === 'synced');
ok('failed shown when it is all there is', musicxStatusFor({artist:'Mini Trees',song:'Hard To Love'}, E) === 'failed');
ok('never requested -> null', musicxStatusFor({artist:'Nobody',song:'Nothing'}, E) === null);
ok('entry without artist/title ignored', musicxStatusFor({artist:'',song:''}, E) === null);
ok('unknown status ignored', musicxStatusFor({artist:'A',song:'B'}, E) === null);
ok('null entries tolerated', musicxStatusFor({artist:'A',song:'B'}, null) === null);

ok('title: radio edit', musicxCleanTitle('Bruises (Radio Edit)') === 'Bruises');
ok('title: feat + bracket', musicxCleanTitle('Song [Remastered 2011] (feat. X)') === 'Song');
ok('title: dash suffix', musicxCleanTitle('Song - Radio Edit') === 'Song');
ok('title: plain unchanged', musicxCleanTitle("'79 Pbj & Wine") === "'79 Pbj & Wine");

const lrc = parseLrc('[ar:X]\n[00:12.50] hello\n[01:02.00][02:00.25] chorus\n\nno stamp\n[00:05.00]');
ok('lrc ignores header/untimed lines', lrc.length === 4);
ok('lrc sorted by time', lrc.map(x => x.t).join() === '5,12.5,62,120.25');
ok('lrc repeated stamps expand', lrc.filter(x => x.text === 'chorus').length === 2);

const times = [10, 20, 30];
ok('line index before first', musicxLineIndex(times, 5) === -1);
ok('line index exactly at a line', musicxLineIndex(times, 20) === 1);
ok('line index between', musicxLineIndex(times, 29.9) === 1);
ok('line index after last', musicxLineIndex(times, 999) === 2);

(async () => {
  const mk = (list) => { globalThis.fetch = async () => ({ ok: true, json: async () => list }); };
  globalThis.AbortSignal = { timeout: () => undefined };
  const rec = (trackName, artistName, extra) => Object.assign({ trackName, artistName, instrumental: false }, extra);

  mk([rec('Other Song','Chairlift',{syncedLyrics:'[00:01.00] wrong'}), rec('Bruises','Chairlift',{syncedLyrics:'[00:02.00] right'})]);
  let r = await fetchMusicxLyrics({ id:'a', artist:'Chairlift', song:'Bruises (Radio Edit)' });
  ok('picks the matching track, not the first result', r.found && r.synced[0].text === 'right');

  mk([rec('Bruises','Someone Else',{syncedLyrics:'[00:02.00] wrong artist'})]);
  r = await fetchMusicxLyrics({ id:'b', artist:'Chairlift', song:'Bruises' });
  ok('wrong artist is rejected (wrong lyrics are worse than none)', r.found === false);

  mk([rec('Bruises','Chairlift',{plainLyrics:'just text'}), rec('Bruises','Chairlift',{syncedLyrics:'[00:03.00] sync'})]);
  r = await fetchMusicxLyrics({ id:'c', artist:'Chairlift', song:'Bruises' });
  ok('synced preferred over plain', r.synced.length === 1);

  mk([rec('Bruises','Chairlift',{instrumental:true, syncedLyrics:'[00:03.00] x'})]);
  r = await fetchMusicxLyrics({ id:'d', artist:'Chairlift', song:'Bruises' });
  ok('instrumental skipped', r.found === false);

  mk([]);
  r = await fetchMusicxLyrics({ id:'e', artist:'Nobody', song:'Nothing' });
  ok('empty result is cached as not-found', r.found === false && MUSICX_LYRICS.e === r);

  globalThis.fetch = async () => { throw new Error('offline'); };
  let threw = false;
  try { await fetchMusicxLyrics({ id:'f', artist:'A', song:'B' }); } catch (e) { threw = true; }
  ok('network failure throws and is not cached', threw && !MUSICX_LYRICS.f);

  console.log(JSON.stringify(out));
})();
"""

if not shutil.which("node"):
    print("SKIP: node not found")
    sys.exit(0)
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
    f.write(JS % {"code": code})
    path = f.name
r = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
os.unlink(path)
if r.returncode != 0:
    print("FAIL harness:", r.stderr.strip()[-500:])
    sys.exit(1)
res = json.loads(r.stdout.strip().splitlines()[-1])
bad = [k for k, v in res.items() if not v]
for k, v in res.items():
    print(("ok   " if v else "FAIL ") + k)
print(f"\n{len(bad)} failure(s)" if bad else "\nall passed")
sys.exit(1 if bad else 0)
