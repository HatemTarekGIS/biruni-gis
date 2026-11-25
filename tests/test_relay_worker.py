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

// roomPerHour = 600: 600 messages pass, 601st closes with 1013, join while blocked closes 1013
{
  const h = new Hub({ roomPerHour: 600 });
  for (let s = 0; s < 31; s++) {
    assert.equal(h.join('rs' + s, T1, 1000).close, undefined);
  }
  for (let i = 0; i < 600; i++) {
    const sid = 'rs' + Math.floor(i / 20);
    assert.equal(h.message(sid, FRAME, 1000).close, undefined);
  }
  // 601st message hits empty room bucket -> 1013 and blocks room for 600000 ms
  assert.deepEqual(h.message('rs30', FRAME, 1000), { close: 1013 });
  assert.deepEqual(h.join('lateJoin', T1, 1000 + 300000), { close: 1013 });
  // After 10 minutes (600000 ms), room unblocks and refilled tokens allow joining & sending
  assert.equal(h.join('afterBlock', T1, 1000 + 600000).close, undefined);
  assert.equal(h.message('afterBlock', FRAME, 1000 + 600000).close, undefined);
}

// roomPerHour = 600: 1 token is back after 6 s (without triggering the 10-min block)
{
  const h = new Hub({ roomPerHour: 600 });
  for (let s = 0; s < 30; s++) {
    assert.equal(h.join('t' + s, T1, 1000).close, undefined);
  }
  for (let i = 0; i < 600; i++) {
    const sid = 't' + Math.floor(i / 20);
    assert.equal(h.message(sid, FRAME, 1000).close, undefined);
  }
  assert.equal(h.join('t30', T1, 7000).close, undefined);
  assert.equal(h.message('t30', FRAME, 7000).close, undefined);
  assert.deepEqual(h.message('t30', FRAME, 7000), { close: 1013 });
}

// Default Hub() has no room or day budget
{
  const h = new Hub();
  for (let s = 0; s < 35; s++) {
    assert.equal(h.join('d' + s, T1, 1000).close, undefined);
  }
  for (let i = 0; i < 650; i++) {
    const sid = 'd' + Math.floor(i / 20);
    assert.equal(h.message(sid, FRAME, 1000).close, undefined);
  }
}

// dayBudget closes messages and joins with 1013 and resets on the next UTC day
{
  const h = new Hub({ dayBudget: 5 });
  const day0 = Date.UTC(2026, 10, 18, 23, 59, 0);
  const day1 = Date.UTC(2026, 10, 19, 0, 0, 1);
  assert.equal(h.join('u1', T1, day0).close, undefined);
  for (let i = 0; i < 5; i++) {
    assert.equal(h.message('u1', FRAME, day0 + i).close, undefined);
  }
  assert.deepEqual(h.message('u1', FRAME, day0 + 10), { close: 1013 });
  assert.deepEqual(h.join('u2', T2, day0 + 20), { close: 1013 });
  assert.equal(h.join('u2', T2, day1).close, undefined);
  assert.equal(h.message('u2', FRAME, day1).close, undefined);
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

# ---- 1b. node tests on worker.js mailbox routes --------------------------------------------------
MB_NODE_SCRIPT = r"""
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import worker from '../relay/worker.js';
import { FakeR2 } from './mb_fake_r2.mjs';

const SECRET = crypto.randomBytes(32).toString('base64url');
const ROOM = '0123456789abcdef';
const ROOM2 = 'fedcba9876543210';
const DEV = 'dev_phone-01';
const NAME = '000001-0123456789ab.bfp';
const KEY = `o/${ROOM}/${DEV}/${NAME}`;

function makeToken(secret, purpose, room) {
  return crypto.createHmac('sha256', Buffer.from(secret, 'utf8'))
    .update(`${purpose}|${room}`, 'utf8')
    .digest('base64url');
}

function makePutSig(secret, key, size, sha, e) {
  return crypto.createHmac('sha256', Buffer.from(secret, 'utf8'))
    .update(`PUT|${key}|${size}|${sha}|${e}`, 'utf8')
    .digest('base64url');
}

const UT = makeToken(SECRET, 'up', ROOM);
const OT = makeToken(SECRET, 'of', ROOM);
const VW = makeToken(SECRET, 'vw', ROOM);
const UT2 = makeToken(SECRET, 'up', ROOM2);
const OT2 = makeToken(SECRET, 'of', ROOM2);

const seenBodies = [];

async function call(env, path, { method = 'GET', headers = {}, body } = {}) {
  const h = new Headers(headers);
  let reqBody = body;
  if (body !== undefined && body !== null) {
    const buf = Buffer.isBuffer(body) ? body : Buffer.from(String(body), 'utf8');
    if (!h.has('Content-Length')) {
      h.set('Content-Length', String(buf.length));
    }
    reqBody = buf;
  }
  const req = new Request(`https://biruni-relay.test.workers.dev${path}`, {
    method,
    headers: h,
    body: reqBody,
  });
  const res = await worker.fetch(req, env);
  const buf = Buffer.from(await res.arrayBuffer());
  const text = buf.toString('utf8');
  seenBodies.push(text);
  let data = null;
  try {
    data = JSON.parse(text);
  } catch {}
  return { status: res.status, headers: res.headers, buf, text, data };
}

async function test(name, fn) {
  try {
    await fn();
    console.log(`PASS ${name}`);
  } catch (err) {
    console.log(`FAIL ${name}: ${err && err.message ? err.message : err}`);
    process.exitCode = 1;
  }
}

await test('v_without_binding', async () => {
  const env = {};
  const rv = await call(env, '/v');
  assert.equal(rv.status, 200);
  assert.deepEqual(rv.data, { rel: 2, mb: false });
  assert.equal(rv.headers.get('Cache-Control'), 'no-store');

  const rt = await call(env, '/t');
  assert.equal(rt.status, 200);
  assert.equal(typeof rt.data.now, 'number');
  assert.equal(Math.abs(rt.data.now - Date.now()) < 5000, true);

  const rs = await call(env, '/mb/sign', {
    method: 'POST',
    headers: { Authorization: `Bearer ${UT}` },
    body: JSON.stringify({ room: ROOM, dev: DEV, name: NAME, size: 4, sha: 'a'.repeat(64) }),
  });
  assert.equal(rs.status, 501);
  assert.deepEqual(rs.data, { error: 'no-mailbox' });

  const rp = await call(env, `/mb/${KEY}?e=9999999999&z=4&h=${'a'.repeat(64)}&s=${UT}`, {
    method: 'PUT',
    body: Buffer.from('abcd'),
  });
  assert.equal(rp.status, 501);

  const rl = await call(env, `/mb/list?room=${ROOM}`, {
    headers: { Authorization: `Bearer ${OT}` },
  });
  assert.equal(rl.status, 501);
});

await test('v_binding_without_secret', async () => {
  const r1 = await call({ MB: new FakeR2() }, '/v');
  assert.equal(r1.status, 200);
  assert.deepEqual(r1.data, { rel: 2, mb: false });

  const r2 = await call({ MB_SECRET: SECRET }, '/v');
  assert.equal(r2.status, 200);
  assert.deepEqual(r2.data, { rel: 2, mb: false });
});

await test('relay_route_unchanged', async () => {
  const env = { MB: new FakeR2(), MB_SECRET: SECRET };
  const topic = 'biruni' + '0'.repeat(40);
  const rRelay = await call(env, `/r/${topic}`);
  assert.equal(rRelay.status, 426);
  assert.equal(rRelay.text, 'Upgrade Required');

  const r404 = await call(env, '/x');
  assert.equal(r404.status, 404);
  assert.equal(r404.text, 'Not found');

  const rVSub = await call(env, '/v/extra');
  assert.equal(rVSub.status, 404);
});

const env = { MB: new FakeR2(), MB_SECRET: SECRET };
const payload1 = Buffer.from('BFP1-test-payload-bytes-001', 'utf8');
const sha1 = crypto.createHash('sha256').update(payload1).digest('hex');

await test('sign_and_put_ok', async () => {
  const rv = await call(env, '/v');
  assert.deepEqual(rv.data, { rel: 2, mb: true });

  const rs = await call(env, '/mb/sign', {
    method: 'POST',
    headers: { Authorization: `Bearer ${UT}` },
    body: JSON.stringify({ room: ROOM, dev: DEV, name: NAME, size: payload1.length, sha: sha1 }),
  });
  assert.equal(rs.status, 200);
  assert.equal(typeof rs.data.url, 'string');
  assert.equal(rs.data.url.startsWith(`/mb/${KEY}?`), true);

  const rp = await call(env, rs.data.url, {
    method: 'PUT',
    body: payload1,
  });
  assert.equal(rp.status, 201);
  assert.deepEqual(rp.data, { ok: true });

  const rl = await call(env, `/mb/list?room=${ROOM}`, {
    headers: { Authorization: `Bearer ${OT}` },
  });
  assert.equal(rl.status, 200);
  assert.equal(rl.data.objects.length, 1);
  assert.equal(rl.data.objects[0].key, KEY);
  assert.equal(rl.data.objects[0].size, payload1.length);
  assert.equal(rl.data.objects[0].sha, sha1);
  assert.equal(rl.data.more, false);

  const rg = await call(env, `/mb/${KEY}`, {
    headers: { Authorization: `Bearer ${OT}` },
  });
  assert.equal(rg.status, 200);
  assert.equal(Buffer.compare(rg.buf, payload1), 0);
});

await test('expired_url', async () => {
  const nameExp = '000002-0123456789ab.bfp';
  const keyExp = `o/${ROOM}/${DEV}/${nameExp}`;
  const nowSec = Math.floor(Date.now() / 1000);

  const pastE = nowSec - 1;
  const sigPast = makePutSig(SECRET, keyExp, payload1.length, sha1, pastE);
  const rPast = await call(env, `/mb/${keyExp}?e=${pastE}&z=${payload1.length}&h=${sha1}&s=${sigPast}`, {
    method: 'PUT',
    body: payload1,
  });
  assert.equal(rPast.status, 403);

  const futureE = nowSec + 601;
  const sigFut = makePutSig(SECRET, keyExp, payload1.length, sha1, futureE);
  const rFut = await call(env, `/mb/${keyExp}?e=${futureE}&z=${payload1.length}&h=${sha1}&s=${sigFut}`, {
    method: 'PUT',
    body: payload1,
  });
  assert.equal(rFut.status, 403);
  assert.equal(await env.MB.head(keyExp), null);
});

await test('wrong_size', async () => {
  const nameSz = '000003-0123456789ab.bfp';
  const keySz = `o/${ROOM}/${DEV}/${nameSz}`;
  const rs = await call(env, '/mb/sign', {
    method: 'POST',
    headers: { Authorization: `Bearer ${UT}` },
    body: JSON.stringify({ room: ROOM, dev: DEV, name: nameSz, size: payload1.length, sha: sha1 }),
  });
  assert.equal(rs.status, 200);

  // Body one byte short -> Content-Length != z -> 400
  const shortBuf = payload1.subarray(0, payload1.length - 1);
  const rShort = await call(env, rs.data.url, {
    method: 'PUT',
    body: shortBuf,
  });
  assert.equal(rShort.status, 400);

  // Changed z in query -> signature mismatch -> 403
  const badZUrl = rs.data.url.replace(`z=${payload1.length}`, `z=${payload1.length - 1}`);
  const rBadZ = await call(env, badZUrl, {
    method: 'PUT',
    body: shortBuf,
  });
  assert.equal(rBadZ.status, 403);
  assert.equal(await env.MB.head(keySz), null);
});

await test('wrong_sha', async () => {
  const nameSha = '000004-0123456789ab.bfp';
  const keySha = `o/${ROOM}/${DEV}/${nameSha}`;
  const rs = await call(env, '/mb/sign', {
    method: 'POST',
    headers: { Authorization: `Bearer ${UT}` },
    body: JSON.stringify({ room: ROOM, dev: DEV, name: nameSha, size: payload1.length, sha: sha1 }),
  });
  assert.equal(rs.status, 200);

  const otherBytes = Buffer.alloc(payload1.length, 0x7a);
  const rBad = await call(env, rs.data.url, {
    method: 'PUT',
    body: otherBytes,
  });
  assert.equal(rBad.status, 400);
  assert.equal(await env.MB.head(keySha), null);
});

await test('overwrite_refused', async () => {
  const otherPayload = Buffer.from('BFP1-different-content-9999', 'utf8');
  const otherSha = crypto.createHash('sha256').update(otherPayload).digest('hex');

  const rsDiff = await call(env, '/mb/sign', {
    method: 'POST',
    headers: { Authorization: `Bearer ${UT}` },
    body: JSON.stringify({ room: ROOM, dev: DEV, name: NAME, size: otherPayload.length, sha: otherSha }),
  });
  assert.equal(rsDiff.status, 200);

  const r409 = await call(env, rsDiff.data.url, {
    method: 'PUT',
    body: otherPayload,
  });
  assert.equal(r409.status, 409);

  // Stored bytes remain payload1
  const rg = await call(env, `/mb/${KEY}`, {
    headers: { Authorization: `Bearer ${OT}` },
  });
  assert.equal(rg.status, 200);
  assert.equal(Buffer.compare(rg.buf, payload1), 0);

  // Same content gives 200 {ok:true, had:true}
  const rsSame = await call(env, '/mb/sign', {
    method: 'POST',
    headers: { Authorization: `Bearer ${UT}` },
    body: JSON.stringify({ room: ROOM, dev: DEV, name: NAME, size: payload1.length, sha: sha1 }),
  });
  assert.equal(rsSame.status, 200);
  const r200 = await call(env, rsSame.data.url, {
    method: 'PUT',
    body: payload1,
  });
  assert.equal(r200.status, 200);
  assert.deepEqual(r200.data, { ok: true, had: true });
});

await test('viewer_token_refused', async () => {
  const signBody = JSON.stringify({ room: ROOM, dev: DEV, name: NAME, size: payload1.length, sha: sha1 });

  // vw token on sign -> 403
  const rVw = await call(env, '/mb/sign', {
    method: 'POST',
    headers: { Authorization: `Bearer ${VW}` },
    body: signBody,
  });
  assert.equal(rVw.status, 403);

  // ot on sign -> 403
  const rOt = await call(env, '/mb/sign', {
    method: 'POST',
    headers: { Authorization: `Bearer ${OT}` },
    body: signBody,
  });
  assert.equal(rOt.status, 403);

  // ut on list -> 403
  const rUtList = await call(env, `/mb/list?room=${ROOM}`, {
    headers: { Authorization: `Bearer ${UT}` },
  });
  assert.equal(rUtList.status, 403);

  // ut on GET /mb/o -> 403
  const rUtGet = await call(env, `/mb/${KEY}`, {
    headers: { Authorization: `Bearer ${UT}` },
  });
  assert.equal(rUtGet.status, 403);

  // another room's ut on sign -> 403
  const rUt2 = await call(env, '/mb/sign', {
    method: 'POST',
    headers: { Authorization: `Bearer ${UT2}` },
    body: signBody,
  });
  assert.equal(rUt2.status, 403);

  // another room's ot on list / GET -> 403
  const rOt2List = await call(env, `/mb/list?room=${ROOM}`, {
    headers: { Authorization: `Bearer ${OT2}` },
  });
  assert.equal(rOt2List.status, 403);

  // no bearer -> 401
  const rNoAuthSign = await call(env, '/mb/sign', {
    method: 'POST',
    body: signBody,
  });
  assert.equal(rNoAuthSign.status, 401);

  const rNoAuthList = await call(env, `/mb/list?room=${ROOM}`);
  assert.equal(rNoAuthList.status, 401);

  const rNoAuthGet = await call(env, `/mb/${KEY}`);
  assert.equal(rNoAuthGet.status, 401);
});

await test('no_delete_path', async () => {
  const rd1 = await call(env, `/mb/${KEY}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${UT}` },
  });
  assert.equal(rd1.status, 405);

  const rd2 = await call(env, `/mb/${KEY}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${OT}` },
  });
  assert.equal(rd2.status, 405);

  const rd3 = await call(env, `/mb/rc/${ROOM}/${DEV}.json`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${OT}` },
  });
  assert.equal(rd3.status, 405);
});

await test('receipts', async () => {
  const rcPath = `/mb/rc/${ROOM}/${DEV}.json`;
  const rc1 = JSON.stringify({ v: 1, room: ROOM, dev: DEV, items: [[1, 1, '01234567']] });
  const rc2 = JSON.stringify({ v: 1, room: ROOM, dev: DEV, items: [[1, 2, '01234567']] });

  // ot PUT rc twice is ok
  const rp1 = await call(env, rcPath, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${OT}` },
    body: rc1,
  });
  assert.equal(rp1.status, 200);

  const rp2 = await call(env, rcPath, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${OT}` },
    body: rc2,
  });
  assert.equal(rp2.status, 200);

  // ut PUT gives 403
  const rpUt = await call(env, rcPath, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${UT}` },
    body: rc1,
  });
  assert.equal(rpUt.status, 403);

  // ut GET gives 200
  const rgUt = await call(env, rcPath, {
    headers: { Authorization: `Bearer ${UT}` },
  });
  assert.equal(rgUt.status, 200);
  assert.equal(rgUt.text, rc2);

  // ot GET also gives 200
  const rgOt = await call(env, rcPath, {
    headers: { Authorization: `Bearer ${OT}` },
  });
  assert.equal(rgOt.status, 200);
  assert.equal(rgOt.text, rc2);

  // another room's ut GET gives 403
  const rgUt2 = await call(env, rcPath, {
    headers: { Authorization: `Bearer ${UT2}` },
  });
  assert.equal(rgUt2.status, 403);

  // 300 KB receipt gives 413
  const bigRc = Buffer.alloc(300 * 1024, 0x61);
  const rpBig = await call(env, rcPath, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${OT}` },
    body: bigRc,
  });
  assert.equal(rpBig.status, 413);
});

await test('cors', async () => {
  const goodOrigin = 'https://photos.biruni-gis.com';
  const rOpt = await call(env, '/mb/sign', {
    method: 'OPTIONS',
    headers: { Origin: goodOrigin },
  });
  assert.equal(rOpt.status, 204);
  assert.equal(rOpt.headers.get('Access-Control-Allow-Origin'), goodOrigin);
  assert.equal(rOpt.headers.get('Access-Control-Allow-Methods'), 'GET, POST, PUT, OPTIONS');
  assert.equal(rOpt.headers.get('Access-Control-Allow-Headers'), 'Authorization, Content-Type');
  assert.equal(rOpt.headers.get('Access-Control-Max-Age'), '86400');

  const rEvilOpt = await call(env, '/mb/sign', {
    method: 'OPTIONS',
    headers: { Origin: 'https://evil.example' },
  });
  assert.equal(rEvilOpt.status, 403);
  assert.equal(rEvilOpt.headers.get('Access-Control-Allow-Origin'), null);

  const rEvilGet = await call(env, '/v', {
    headers: { Origin: 'https://evil.example' },
  });
  assert.equal(rEvilGet.status, 403);
  assert.equal(rEvilGet.headers.get('Access-Control-Allow-Origin'), null);
});

await test('no_secret_echo', async () => {
  for (const b of seenBodies) {
    assert.equal(b.includes(SECRET), false, 'response body leaked SECRET');
    assert.equal(b.includes(UT), false, 'response body leaked UT');
    assert.equal(b.includes(OT), false, 'response body leaked OT');
    assert.equal(b.includes(VW), false, 'response body leaked VW');
    assert.equal(b.includes(UT2), false, 'response body leaked UT2');
    assert.equal(b.includes(OT2), false, 'response body leaked OT2');
  }
});
"""

p_mb = subprocess.run(
    [NODE, "--input-type=module", "-e", MB_NODE_SCRIPT],
    cwd=HERE,
    capture_output=True,
    text=True,
    encoding="utf-8",
    timeout=30,
)
for line in p_mb.stdout.splitlines():
    if line.startswith("PASS "):
        check(True, line[5:])
    elif line.startswith("FAIL "):
        check(False, line[5:])
if p_mb.returncode != 0 and "FAIL " not in p_mb.stdout:
    check(False, f"mailbox node tests failed ({p_mb.stderr.strip() or p_mb.stdout.strip()})")

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
check('ROOM_PER_HOUR = "600"' in toml and 'DAY_BUDGET = "1600000"' in toml,
      "wrangler.toml sets ROOM_PER_HOUR=600 and DAY_BUDGET=1600000")
check("r2_buckets" not in toml and "MB_SECRET" not in toml,
      "wrangler.toml has no r2_buckets or MB_SECRET")
with open(os.path.join(RELAY, "worker.js"), encoding="utf-8") as f:
    worker_js = f.read()
check("ROOM_PER_HOUR" in worker_js and "DAY_BUDGET" in worker_js,
      "worker.js forwards ROOM_PER_HOUR and DAY_BUDGET to Hub")

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
