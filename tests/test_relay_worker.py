"""Tests for the Cloudflare Worker Biruni relay (relay/hub.js, worker.js, wrangler.toml, deploy_relay.ps1).

Run:  engine\\bin\\python-qgis.bat tests\\test_relay_worker.py
"""
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
RELAY = os.path.join(ROOT, "relay")
DEPLOY_PS1 = os.path.join(ROOT, "scripts", "deploy_relay.ps1")
NODE = shutil.which("node") or next(
    (p for p in (r"C:\Program Files\nodejs\node.exe", r"C:\Program Files (x86)\nodejs\node.exe") if os.path.isfile(p)),
    "node",
)
POWERSHELL = shutil.which("powershell") or r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


# ---- 1. node tests on relay/hub.js ---------------------------------------------------------------
NODE_SCRIPT = r"""
import assert from 'node:assert/strict';
import { Hub } from './hub.js';

const T1 = 'biruni' + '0'.repeat(40);
const T2 = 'biruni' + 'a'.repeat(40);
const FRAME = 'A'.repeat(32);

// Invalid topic -> 1008
{
  const h = new Hub();
  assert.deepEqual(h.join('s1', 'bad', 1000), { close: 1008 });
  assert.deepEqual(h.join('s1', 'biruni' + '0'.repeat(39), 1000), { close: 1008 });
  assert.deepEqual(h.join('s1', 'biruni' + 'g'.repeat(40), 1000), { close: 1008 });
}

// No echo to sender, fan-out to peers
{
  const h = new Hub();
  assert.deepEqual(h.join('s1', T1, 1000), { to: [], replay: [] });
  assert.deepEqual(h.join('s2', T1, 1000), { to: [], replay: [] });
  assert.deepEqual(h.join('s3', T1, 1000), { to: [], replay: [] });
  const r = h.message('s1', FRAME, 1010);
  assert.deepEqual(r.to.sort(), ['s2', 's3']);
  assert.equal(r.to.includes('s1'), false);
}

// Replay of last 60 s, capped at 20, older than 60 s dropped
{
  const h = new Hub();
  h.join('s1', T1, 1000);
  h.message('s1', 'OldFrame' + 'B'.repeat(24), 1000);
  for (let i = 0; i < 25; i++) {
    const f = 'Msg_' + String(i).padStart(2, '0') + '_' + 'C'.repeat(24);
    assert.equal(h.message('s1', f, 50000 + i * 200).close, undefined);
  }
  const j = h.join('s2', T1, 70000);
  assert.equal(j.replay.length, 20);
  assert.equal(j.replay[0].startsWith('Msg_05_'), true);
  assert.equal(j.replay[19].startsWith('Msg_24_'), true);
  // After >60s past all messages, replay is empty
  const j2 = h.join('s3', T1, 140000);
  assert.deepEqual(j2.replay, []);
}

// Close codes 1008 (bad frame) and 1009 (>90000)
{
  const h = new Hub();
  h.join('s1', T1, 1000);
  assert.deepEqual(h.message('s1', 'short', 1000), { close: 1008 });
  assert.deepEqual(h.message('s1', 'A'.repeat(23), 1000), { close: 1008 });
  assert.deepEqual(h.message('s1', 'bad/chars+here===' + 'A'.repeat(20), 1000), { close: 1008 });
  assert.deepEqual(h.message('s1', 12345, 1000), { close: 1008 });
  assert.deepEqual(h.message('s1', 'A'.repeat(90001), 1000), { close: 1009 });
  assert.equal(h.message('s1', 'A'.repeat(90000), 1000).close, undefined);
}

// Rate limit: 5/s, burst 20 -> 1013
{
  const h = new Hub();
  h.join('s1', T1, 1000);
  for (let i = 0; i < 20; i++) {
    assert.equal(h.message('s1', FRAME, 1000).close, undefined);
  }
  assert.deepEqual(h.message('s1', FRAME, 1000), { close: 1013 });
  // After 200 ms, 1 token refilled
  assert.equal(h.message('s1', FRAME, 1200).close, undefined);
}

// Room cap: 64 sockets -> 65th closes 1013; leave frees slot
{
  const h = new Hub();
  for (let i = 0; i < 64; i++) {
    assert.equal(h.join('u' + i, T1, 1000).close, undefined);
  }
  assert.deepEqual(h.join('u64', T1, 1000), { close: 1013 });
  h.leave('u0');
  assert.equal(h.join('u64', T1, 1000).close, undefined);
}

// 2000 rooms cap -> 2001st closes 1013
{
  const h = new Hub();
  for (let i = 0; i < 2000; i++) {
    const topic = 'biruni' + i.toString(16).padStart(40, '0');
    assert.equal(h.join('r' + i, topic, 1000).close, undefined);
  }
  assert.deepEqual(h.join('extra', T2, 1000), { close: 1013 });
}

console.log('HUB_JS_OK');
"""

p_hub = subprocess.run(
    [NODE, "--input-type=module", "-e", NODE_SCRIPT],
    cwd=RELAY,
    capture_output=True,
    text=True,
    encoding="utf-8",
    timeout=30,
)
check(p_hub.returncode == 0 and "HUB_JS_OK" in p_hub.stdout,
      f"hub.js pure logic tests pass ({p_hub.stderr.strip() or p_hub.stdout.strip()})")

# ---- 2. node --check worker.js ------------------------------------------------------------------
p_chk = subprocess.run(
    [NODE, "--check", "worker.js"],
    cwd=RELAY,
    capture_output=True,
    text=True,
    encoding="utf-8",
    timeout=15,
)
check(p_chk.returncode == 0, f"node --check worker.js passes ({p_chk.stderr.strip()})")

# ---- 3. wrangler.toml settings -------------------------------------------------------------------
with open(os.path.join(RELAY, "wrangler.toml"), encoding="utf-8") as f:
    toml = f.read()
check("new_sqlite_classes" in toml and "custom_domain" in toml,
      "wrangler.toml contains new_sqlite_classes and custom_domain")

# ---- 4. No token-like string in relay\ or deploy_relay.ps1 --------------------------------------
TOKEN_RES = [
    re.compile(r"cfat_[A-Za-z0-9_-]{10,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_.-]{16,}"),
    re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9_-]{36,}(?![A-Za-z0-9_])"),
]
# Allow known harmless identifiers/comments
ALLOW_SUBSTR = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "WebSocketRequestResponsePair")

token_hits = []
scan_files = [DEPLOY_PS1] + [
    os.path.join(RELAY, fn) for fn in sorted(os.listdir(RELAY))
    if os.path.isfile(os.path.join(RELAY, fn))
]
for fp in scan_files:
    with open(fp, encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            for rx in TOKEN_RES:
                for m in rx.finditer(line):
                    tok = m.group(0)
                    if any(a in tok for a in ALLOW_SUBSTR):
                        continue
                    token_hits.append((os.path.basename(fp), lineno, tok))

check(not token_hits, f"no token-like string in relay\\ or deploy_relay.ps1 ({token_hits})")

# ---- 5. deploy_relay.ps1 exits non-zero when env vars empty --------------------------------------
env = dict(os.environ, CLOUDFLARE_API_TOKEN="", CLOUDFLARE_ACCOUNT_ID="")
p_ps = subprocess.run(
    [POWERSHELL, "-NoProfile", "-File", DEPLOY_PS1],
    cwd=ROOT,
    env=env,
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    timeout=30,
)
check(p_ps.returncode != 0, f"deploy_relay.ps1 exits non-zero with empty env vars (rc={p_ps.returncode})")

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
