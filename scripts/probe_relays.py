"""Dev utility: probe Nostr relays and MQTT brokers (pool + candidates) via bridge._core() in Node.

Needs network access; not part of the automated test suite.
Usage:  engine\\bin\\python-qgis.bat scripts\\probe_relays.py
"""
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
sys.path.insert(0, os.path.join(ROOT, "app", "python"))

from biruni.field_photos import bridge, relay_pool  # noqa: E402

NODE_PROBE = r"""
const targets = JSON.parse(process.argv[process.argv.length-1]);

function withTimeout(ms, label, fn) {
  return new Promise((resolve) => {
    const start = Date.now();
    let done = false;
    const finish = (ok, detail) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve({ ok, ms: Date.now() - start, detail: detail || '' });
    };
    const timer = setTimeout(() => finish(false, 'timeout'), ms);
    Promise.resolve(fn(finish)).catch((e) => finish(false, e && e.message ? e.message : String(e)));
  });
}

function probeNostr(url) {
  return withTimeout(10000, url, async (finish) => {
    const topic = 'biruni_probe_' + rid();
    const payload = 'probe_' + rid();
    const ev = await nostrEvent(payload, topic);
    const subId = 'p' + rid();
    let rx, tx, sent = false;
    let cleaned = false;
    const cleanup = () => {   // once: close() of a failed socket fires onerror again
      if (cleaned) return;
      cleaned = true;
      for (const w of [rx, tx]) {
        if (!w) continue;
        w.onerror = w.onclose = w.onmessage = w.onopen = null;
        try { w.close(); } catch (e) {}
      }
    };
    const trySend = () => {
      if (!sent && rx && rx.readyState === 1 && tx && tx.readyState === 1) {
        sent = true;
        setTimeout(() => {
          try { if (tx.readyState === 1) tx.send(JSON.stringify(['EVENT', ev])); } catch (e) {}
        }, 200);
      }
    };
    rx = new WebSocket(url);
    tx = new WebSocket(url);
    rx.onopen = () => {
      rx.send(JSON.stringify(['REQ', subId, { kinds: [BR.kind], '#t': [topic] }]));
      trySend();
    };
    tx.onopen = trySend;
    rx.onmessage = (e) => {
      try {
        const m = JSON.parse(e.data);
        if (m[0] === 'EVENT' && m[2] && m[2].content === payload) {
          cleanup();
          finish(true, 'ok');
        }
      } catch (x) {}
    };
    tx.onmessage = (e) => {
      try {
        const m = JSON.parse(e.data);
        if (m[0] === 'OK' && m[1] === ev.id && m[2] === false) {
          cleanup();
          finish(false, 'rejected: ' + (m[3] || 'OK false'));
        }
      } catch (x) {}
    };
    rx.onerror = () => { cleanup(); finish(false, 'rx error'); };
    tx.onerror = () => { cleanup(); finish(false, 'tx error'); };
  });
}

function probeMqtt(url) {
  return withTimeout(10000, url, async (finish) => {
    const topic = 'biruni/probe/' + rid();
    const payload = 'probe_' + rid();
    let rx, tx, rxReady = false, txReady = false, sent = false;
    let rxBuf = new Uint8Array(0), txBuf = new Uint8Array(0);
    let cleaned = false;
    const cleanup = () => {   // once: close() of a failed socket fires onerror again
      if (cleaned) return;
      cleaned = true;
      for (const w of [rx, tx]) {
        if (!w) continue;
        w.onerror = w.onclose = w.onmessage = w.onopen = null;
        try { w.close(); } catch (e) {}
      }
    };
    const cid = () => 'bp' + rid().replace(/[^A-Za-z0-9]/g, '');
    const trySend = () => {
      if (!sent && rxReady && txReady) {
        sent = true;
        try { tx.send(MQ.publish(topic, payload, 1, 2)); } catch (e) {}
      }
    };
    rx = new WebSocket(url, 'mqtt');
    rx.binaryType = 'arraybuffer';
    tx = new WebSocket(url, 'mqtt');
    tx.binaryType = 'arraybuffer';
    rx.onopen = () => { try { rx.send(MQ.connect(cid())); } catch (e) {} };
    tx.onopen = () => { try { tx.send(MQ.connect(cid())); } catch (e) {} };
    rx.onmessage = (e) => {
      const ch = e.data instanceof ArrayBuffer ? new Uint8Array(e.data) : enc.encode(String(e.data || ''));
      rxBuf = cat(rxBuf, ch);
      const { pk, rest } = MQ.parse(rxBuf);
      rxBuf = rest;
      for (const p of pk) {
        if (p.type === 2 && p.body.length >= 2 && p.body[1] === 0) {
          rx.send(MQ.subscribe(1, topic));
        } else if (p.type === 9 && p.body.length >= 3 && p.body[2] !== 0x80) {
          rxReady = true;
          trySend();
        } else if (p.type === 3 && p.body.length >= 2) {
          const tl = (p.body[0] << 8) | p.body[1];
          const qos = (p.flags >> 1) & 3;
          const off = 2 + tl + (qos > 0 ? 2 : 0);
          if (off <= p.body.length && dec.decode(p.body.subarray(off)) === payload) {
            cleanup();
            finish(true, 'ok');
          }
        }
      }
    };
    tx.onmessage = (e) => {
      const ch = e.data instanceof ArrayBuffer ? new Uint8Array(e.data) : enc.encode(String(e.data || ''));
      txBuf = cat(txBuf, ch);
      const { pk, rest } = MQ.parse(txBuf);
      txBuf = rest;
      for (const p of pk) {
        if (p.type === 2 && p.body.length >= 2 && p.body[1] === 0) {
          txReady = true;
          trySend();
        }
      }
    };
    rx.onerror = () => { cleanup(); finish(false, 'rx error'); };
    tx.onerror = () => { cleanup(); finish(false, 'tx error'); };
  });
}

(async () => {
  const results = [];
  for (const t of targets) {
    const res = t.kind === 'nostr' ? await probeNostr(t.url) : await probeMqtt(t.url);
    results.push({ kind: t.kind, group: t.group, url: t.url, ...res });
  }
  console.log(JSON.stringify(results));
  process.exit(0);
})();
"""


def build_targets():
    targets = []
    for u in relay_pool.NOSTR:
        targets.append({"kind": "nostr", "group": "pool", "url": u})
    for u in relay_pool.NOSTR_CANDIDATES:
        targets.append({"kind": "nostr", "group": "candidate", "url": u})
    for u in relay_pool.MQTT:
        targets.append({"kind": "mqtt", "group": "pool", "url": u})
    for u in relay_pool.MQTT_CANDIDATES:
        targets.append({"kind": "mqtt", "group": "candidate", "url": u})
    return targets


def main():
    node = shutil.which("node") or next(
        (p for p in (r"C:\Program Files\nodejs\node.exe",) if os.path.isfile(p)), None
    )
    if not node:
        print("node not found", file=sys.stderr)
        return 1
    targets = build_targets()
    script = bridge._core() + "\n" + NODE_PROBE
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:   # a file, not -e: the script is longer than a Windows command line
        js = os.path.join(tmp, "probe.js")
        with open(js, "w", encoding="utf-8") as f:
            f.write(script)
        proc = subprocess.run(
            [node, js, json.dumps(targets)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
        )
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return proc.returncode
    rows = json.loads(proc.stdout.strip().splitlines()[-1])
    print(f"{'KIND':<6} {'GROUP':<10} {'STATUS':<6} {'MS':>6}  {'URL':<42} DETAIL")
    print("-" * 92)
    for r in rows:
        st = "PASS" if r["ok"] else "FAIL"
        print(f"{r['kind']:<6} {r['group']:<10} {st:<6} {r['ms']:>6}  {r['url']:<42} {r['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
