const TOPIC_RE = /^biruni[0-9a-f]{40}$/;
const FRAME_RE = /^[A-Za-z0-9_-]{24,90000}$/;
const MAX_ROOMS = 2000;
const MAX_ROOM_SOCKETS = 64;
const REPLAY_WINDOW_MS = 60000;
const REPLAY_MAX = 20;
const RATE_PER_SEC = 5;
const BURST = 20;
const MAX_FRAME_LEN = 90000;
const ROOM_BLOCK_MS = 600000;
const HOUR_MS = 3600000;
const DAY_MS = 86400000;

class Hub {
  constructor({ roomPerHour = 0, dayBudget = 0 } = {}) {
    this.rooms = new Map();
    this.sockets = new Map();
    this.roomPerHour = Number(roomPerHour) || 0;
    this.dayBudget = Number(dayBudget) || 0;
    this.day = null;
    this.dayCount = 0;
  }

  _syncDay(now) {
    const d = Math.floor(now / DAY_MS);
    if (d !== this.day) {
      this.day = d;
      this.dayCount = 0;
    }
  }

  _prune(now) {
    for (const [topic, room] of this.rooms) {
      room.msgs = room.msgs.filter((m) => now - m.ts <= REPLAY_WINDOW_MS);
      if (room.blockedUntil && now >= room.blockedUntil) {
        room.blockedUntil = 0;
      }
      if (room.sockets.size === 0 && room.msgs.length === 0 && !room.blockedUntil) {
        this.rooms.delete(topic);
      }
    }
  }

  join(id, topic, now = Date.now()) {
    if (typeof topic !== 'string' || !TOPIC_RE.test(topic)) {
      return { close: 1008 };
    }
    this._syncDay(now);
    if (this.dayBudget > 0 && this.dayCount >= this.dayBudget) {
      return { close: 1013 };
    }
    if (this.sockets.has(id)) {
      this.leave(id);
    }
    let room = this.rooms.get(topic);
    if (room && room.blockedUntil > now) {
      return { close: 1013 };
    }
    if (room && room.blockedUntil && now >= room.blockedUntil) {
      room.blockedUntil = 0;
    }
    if (!room && this.rooms.size >= MAX_ROOMS) {
      this._prune(now);
      if (this.rooms.size >= MAX_ROOMS) {
        return { close: 1013 };
      }
    }
    if (!room) {
      room = {
        sockets: new Set(),
        msgs: [],
        tokens: this.roomPerHour,
        lastRefill: now,
        blockedUntil: 0,
      };
      this.rooms.set(topic, room);
    }
    if (!room.sockets.has(id) && room.sockets.size >= MAX_ROOM_SOCKETS) {
      return { close: 1013 };
    }
    room.sockets.add(id);
    this.sockets.set(id, { topic, tokens: BURST, lastRefill: now });
    room.msgs = room.msgs.filter((m) => now - m.ts <= REPLAY_WINDOW_MS);
    return { to: [], replay: room.msgs.map((m) => m.text) };
  }

  message(id, text, now = Date.now()) {
    const sock = this.sockets.get(id);
    if (!sock) {
      return { close: 1008 };
    }
    if (typeof text !== 'string') {
      return { close: 1008 };
    }
    if (text.length > MAX_FRAME_LEN) {
      return { close: 1009 };
    }
    if (!FRAME_RE.test(text)) {
      return { close: 1008 };
    }
    this._syncDay(now);
    if (this.dayBudget > 0 && this.dayCount >= this.dayBudget) {
      return { close: 1013 };
    }
    const room = this.rooms.get(sock.topic);
    if (!room) {
      return { close: 1008 };
    }
    if (room.blockedUntil > now) {
      return { close: 1013 };
    }
    if (room.blockedUntil && now >= room.blockedUntil) {
      room.blockedUntil = 0;
    }
    const elapsed = Math.max(0, now - sock.lastRefill);
    sock.tokens = Math.min(BURST, sock.tokens + (elapsed * RATE_PER_SEC) / 1000);
    sock.lastRefill = now;
    if (sock.tokens < 1) {
      return { close: 1013 };
    }
    if (this.roomPerHour > 0) {
      const rElapsed = Math.max(0, now - room.lastRefill);
      room.tokens = Math.min(this.roomPerHour, room.tokens + (rElapsed * this.roomPerHour) / HOUR_MS);
      room.lastRefill = now;
      if (room.tokens < 1) {
        room.blockedUntil = now + ROOM_BLOCK_MS;
        return { close: 1013 };
      }
      room.tokens -= 1;
    }
    sock.tokens -= 1;
    if (this.dayBudget > 0) {
      this.dayCount += 1;
    }

    room.msgs = room.msgs.filter((m) => now - m.ts <= REPLAY_WINDOW_MS);
    room.msgs.push({ text, ts: now });
    if (room.msgs.length > REPLAY_MAX) {
      room.msgs.splice(0, room.msgs.length - REPLAY_MAX);
    }
    const to = [];
    for (const sid of room.sockets) {
      if (sid !== id) {
        to.push(sid);
      }
    }
    return { to, replay: [] };
  }

  leave(id) {
    const sock = this.sockets.get(id);
    if (!sock) {
      return { to: [], replay: [] };
    }
    this.sockets.delete(id);
    const room = this.rooms.get(sock.topic);
    if (room) {
      room.sockets.delete(id);
      if (room.sockets.size === 0 && room.msgs.length === 0 && !room.blockedUntil) {
        this.rooms.delete(sock.topic);
      }
    }
    return { to: [], replay: [] };
  }
}

/*
 * Biruni relay Worker (Cloudflare Workers + Durable Objects Hibernation API).
 * Free-tier note: one hub object serves all rooms; at the limit, connections fail,
 * the client backs off for 10 minutes, and the other levels carry on.
 * No logging of topics or IPs.
 */

const PATH_RE = /^\/r\/(biruni[0-9a-f]{40})$/;
const ALLOWED_ORIGINS = new Set(['https://photos.biruni-gis.com', 'https://biruni-gis.com']);
const LOCAL_ORIGIN_RE = /^https:\/\/127\.0\.0\.1:([1-9]\d{0,4})$/;
const ROOM_RE = /^[0-9a-f]{16}$/;
const DEV_RE = /^[A-Za-z0-9_-]{1,64}$/;
const BFP_RE = /^\d{6}-[0-9a-f]{12}\.bfp$/;
const BGP_RE = /^\d{9}-\d{9}-[\w-]{6}\.bgp$/;
const SHA_RE = /^[0-9a-f]{64}$/;
const OBJ_RE = /^\/mb\/o\/([0-9a-f]{16})\/([A-Za-z0-9_-]{1,64})\/([^/]+)$/;
const RC_RE = /^\/mb\/rc\/([0-9a-f]{16})\/([A-Za-z0-9_-]{1,64})\.json$/;
const MB_MAX = { bfp: 6291456, bgp: 122880, rc: 262144 };
const URL_TTL = 600;
const ENC = new TextEncoder();

function originAllowed(origin) {
  if (!origin) return false;
  if (ALLOWED_ORIGINS.has(origin)) return true;
  const m = LOCAL_ORIGIN_RE.exec(origin);
  return !!m && Number(m[1]) <= 65535;
}

function cors(origin) {
  const h = {};
  if (origin && originAllowed(origin)) {
    h['Access-Control-Allow-Origin'] = origin;
    h['Vary'] = 'Origin';
  }
  return h;
}

function json(status, obj, origin) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: {
      'Content-Type': 'application/json',
      'Cache-Control': 'no-store',
      ...cors(origin),
    },
  });
}

function b64uEnc(buf) {
  let s = '';
  const b = new Uint8Array(buf);
  for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function b64uDec(str) {
  if (typeof str !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(str)) return null;
  try {
    const pad = str.replace(/-/g, '+').replace(/_/g, '/') + '=';
    const bin = atob(pad);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  } catch {
    return null;
  }
}

function hmacKey(env) {
  return crypto.subtle.importKey(
    'raw',
    ENC.encode(String(env.MB_SECRET || '')),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign', 'verify']
  );
}

async function tokenOk(env, bearer, purpose, room) {
  if (!ROOM_RE.test(room)) return false;
  const sig = b64uDec(bearer);
  if (!sig) return false;
  const key = await hmacKey(env);
  return crypto.subtle.verify('HMAC', key, sig, ENC.encode(purpose + '|' + room));
}

function getBearer(request) {
  const auth = request.headers.get('Authorization') || '';
  if (!auth.startsWith('Bearer ')) return null;
  const t = auth.slice(7).trim();
  return t || null;
}

function nameMax(name) {
  if (BFP_RE.test(name)) return MB_MAX.bfp;
  if (BGP_RE.test(name)) return MB_MAX.bgp;
  return 0;
}

async function mailbox(request, env, url) {
  const origin = request.headers.get('Origin') || '';
  const method = request.method.toUpperCase();
  const path = url.pathname;

  if (origin && !originAllowed(origin)) {
    return new Response('Forbidden', { status: 403 });
  }
  if (method === 'OPTIONS') {
    return new Response(null, {
      status: 204,
      headers: {
        ...cors(origin),
        'Access-Control-Allow-Methods': 'GET, POST, PUT, OPTIONS',
        'Access-Control-Allow-Headers': 'Authorization, Content-Type',
        'Access-Control-Max-Age': '86400',
      },
    });
  }
  const hasMb = !!(env && env.MB && env.MB_SECRET);
  if (path === '/v') {
    if (method !== 'GET') return json(405, { error: 'method' }, origin);
    return json(200, { rel: 2, mb: hasMb }, origin);
  }
  if (path === '/t') {
    if (method !== 'GET') return json(405, { error: 'method' }, origin);
    return json(200, { now: Date.now() }, origin);
  }
  if (!hasMb) {
    return json(501, { error: 'no-mailbox' }, origin);
  }
  if (method !== 'GET' && method !== 'POST' && method !== 'PUT') {
    return json(405, { error: 'method' }, origin);
  }

  if (path === '/mb/sign') {
    if (method !== 'POST') return json(405, { error: 'method' }, origin);
    const bearer = getBearer(request);
    if (!bearer) return json(401, { error: 'no-auth' }, origin);
    const raw = await request.text();
    if (raw.length > 1024) return json(413, { error: 'too-large' }, origin);
    let body;
    try {
      body = JSON.parse(raw);
    } catch {
      return json(400, { error: 'bad-json' }, origin);
    }
    const { room, dev, name, size, sha } = body || {};
    if (typeof room !== 'string' || !ROOM_RE.test(room)) {
      return json(400, { error: 'bad-fields' }, origin);
    }
    if (!(await tokenOk(env, bearer, 'up', room))) {
      return json(403, { error: 'forbidden' }, origin);
    }
    const max = typeof name === 'string' ? nameMax(name) : 0;
    if (
      typeof dev !== 'string' || !DEV_RE.test(dev) ||
      !max ||
      typeof sha !== 'string' || !SHA_RE.test(sha) ||
      typeof size !== 'number' || !Number.isInteger(size) || size <= 0
    ) {
      return json(400, { error: 'bad-fields' }, origin);
    }
    if (size > max) return json(413, { error: 'too-large' }, origin);
    const e = Math.floor(Date.now() / 1000) + URL_TTL;
    const key = `o/${room}/${dev}/${name}`;
    const hKey = await hmacKey(env);
    const sigBuf = await crypto.subtle.sign('HMAC', hKey, ENC.encode(`PUT|${key}|${size}|${sha}|${e}`));
    const s = b64uEnc(sigBuf);
    return json(200, { url: `/mb/${key}?e=${e}&z=${size}&h=${sha}&s=${s}`, e }, origin);
  }

  if (path === '/mb/list') {
    if (method !== 'GET') return json(405, { error: 'method' }, origin);
    const bearer = getBearer(request);
    if (!bearer) return json(401, { error: 'no-auth' }, origin);
    const room = url.searchParams.get('room') || '';
    if (!ROOM_RE.test(room)) return json(400, { error: 'bad-room' }, origin);
    if (!(await tokenOk(env, bearer, 'of', room))) {
      return json(403, { error: 'forbidden' }, origin);
    }
    const cursor = url.searchParams.get('cursor') || undefined;
    const listed = await env.MB.list({
      prefix: `o/${room}/`,
      cursor,
      limit: 500,
      include: ['customMetadata'],
    });
    const objects = (listed.objects || []).map((o) => ({
      key: o.key,
      size: o.size,
      up: o.uploaded instanceof Date ? o.uploaded.getTime() : Number(o.uploaded) || 0,
      sha: (o.customMetadata && o.customMetadata.sha) || '',
    }));
    return json(200, {
      objects,
      cursor: listed.truncated ? (listed.cursor || '') : '',
      more: !!listed.truncated,
    }, origin);
  }

  const mObj = OBJ_RE.exec(path);
  if (mObj) {
    const [, room, dev, name] = mObj;
    const max = nameMax(name);
    if (!max) return json(400, { error: 'bad-name' }, origin);
    const key = `o/${room}/${dev}/${name}`;

    if (method === 'PUT') {
      const eStr = url.searchParams.get('e') || '';
      const zStr = url.searchParams.get('z') || '';
      const h = url.searchParams.get('h') || '';
      const s = url.searchParams.get('s') || '';
      if (!/^\d+$/.test(eStr) || !/^\d+$/.test(zStr) || !SHA_RE.test(h)) {
        return json(400, { error: 'bad-query' }, origin);
      }
      const e = Number(eStr);
      const z = Number(zStr);
      if (!Number.isInteger(z) || z <= 0) return json(400, { error: 'bad-size' }, origin);
      if (z > max) return json(413, { error: 'too-large' }, origin);
      const sigBytes = b64uDec(s);
      if (!sigBytes) return json(403, { error: 'bad-sig' }, origin);
      const hKey = await hmacKey(env);
      const okSig = await crypto.subtle.verify(
        'HMAC',
        hKey,
        sigBytes,
        ENC.encode(`PUT|${key}|${z}|${h}|${e}`)
      );
      if (!okSig) return json(403, { error: 'bad-sig' }, origin);
      const nowSec = Math.floor(Date.now() / 1000);
      if (e < nowSec || e > nowSec + URL_TTL) {
        return json(403, { error: 'expired' }, origin);
      }
      const cl = request.headers.get('Content-Length');
      if (cl === null || !/^\d+$/.test(cl) || Number(cl) !== z) {
        return json(400, { error: 'bad-length' }, origin);
      }
      const existing = await env.MB.head(key);
      if (existing) {
        const oldSha = existing.customMetadata && existing.customMetadata.sha;
        if (oldSha === h) return json(200, { ok: true, had: true }, origin);
        return json(409, { error: 'exists' }, origin);
      }
      let putRes;
      try {
        putRes = await env.MB.put(key, request.body, {
          sha256: h,
          onlyIf: new Headers({ 'If-None-Match': '*' }),
          customMetadata: { sha: h },
        });
      } catch {
        return json(400, { error: 'bad-sha' }, origin);
      }
      if (!putRes) {
        const cur = await env.MB.head(key);
        if (cur && cur.customMetadata && cur.customMetadata.sha === h) {
          return json(200, { ok: true, had: true }, origin);
        }
        return json(409, { error: 'exists' }, origin);
      }
      return json(201, { ok: true }, origin);
    }

    if (method === 'GET') {
      const bearer = getBearer(request);
      if (!bearer) return json(401, { error: 'no-auth' }, origin);
      if (!(await tokenOk(env, bearer, 'of', room))) {
        return json(403, { error: 'forbidden' }, origin);
      }
      const obj = await env.MB.get(key);
      if (!obj) return json(404, { error: 'not-found' }, origin);
      return new Response(obj.body, {
        status: 200,
        headers: {
          'Content-Type': 'application/octet-stream',
          'Cache-Control': 'no-store',
          ...cors(origin),
        },
      });
    }

    return json(405, { error: 'method' }, origin);
  }

  const mRc = RC_RE.exec(path);
  if (mRc) {
    const [, room, dev] = mRc;
    const key = `rc/${room}/${dev}.json`;
    const bearer = getBearer(request);
    if (!bearer) return json(401, { error: 'no-auth' }, origin);

    if (method === 'PUT') {
      if (!(await tokenOk(env, bearer, 'of', room))) {
        return json(403, { error: 'forbidden' }, origin);
      }
      const buf = await request.arrayBuffer();
      if (buf.byteLength > MB_MAX.rc) {
        return json(413, { error: 'too-large' }, origin);
      }
      await env.MB.put(key, buf);
      return json(200, { ok: true }, origin);
    }

    if (method === 'GET') {
      const okOf = await tokenOk(env, bearer, 'of', room);
      const okUp = okOf || (await tokenOk(env, bearer, 'up', room));
      if (!okUp) return json(403, { error: 'forbidden' }, origin);
      const obj = await env.MB.get(key);
      if (!obj) return json(404, { error: 'not-found' }, origin);
      return new Response(obj.body, {
        status: 200,
        headers: {
          'Content-Type': 'application/json',
          'Cache-Control': 'no-store',
          ...cors(origin),
        },
      });
    }

    return json(405, { error: 'method' }, origin);
  }

  return json(404, { error: 'not-found' }, origin);
}

export class HubDO {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
    this.hub = new Hub({ roomPerHour: +env.ROOM_PER_HOUR || 0, dayBudget: +env.DAY_BUDGET || 0 });
    this.ids = new WeakMap();
    this.sockets = new Map();
    this.ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair('p', 'q'));
  }

  _ensureRoom(topic, now) {
    const list = this.ctx.getWebSockets(topic);
    for (const ws of list) {
      if (this.ids.has(ws)) continue;
      const att = ws.deserializeAttachment();
      if (!att || !att.id) continue;
      this.ids.set(ws, att.id);
      this.sockets.set(att.id, ws);
      this.hub.join(att.id, topic, now);
    }
  }

  _drop(ws) {
    let id = this.ids.get(ws);
    if (!id) {
      const att = ws.deserializeAttachment();
      if (att && att.id) id = att.id;
    }
    if (id) {
      this.hub.leave(id);
      this.sockets.delete(id);
      this.ids.delete(ws);
    }
  }

  async fetch(request) {
    const m = PATH_RE.exec(new URL(request.url).pathname);
    if (!m) return new Response('Not found', { status: 404 });
    const topic = m[1];
    const now = Date.now();
    this._ensureRoom(topic, now);

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    const id = crypto.randomUUID();
    this.ctx.acceptWebSocket(server, [topic]);
    server.serializeAttachment({ id, topic });

    const res = this.hub.join(id, topic, now);
    if (res.close) {
      try { server.close(res.close, ''); } catch {}
      return new Response(null, { status: 101, webSocket: client });
    }
    this.ids.set(server, id);
    this.sockets.set(id, server);
    for (const msg of res.replay) {
      try { server.send(msg); } catch {}
    }
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws, message) {
    const now = Date.now();
    if (!this.ids.has(ws)) {
      const att = ws.deserializeAttachment();
      if (att && att.topic) this._ensureRoom(att.topic, now);
    }
    const id = this.ids.get(ws);
    if (!id) {
      try { ws.close(1008, ''); } catch {}
      return;
    }
    const res = this.hub.message(id, message, now);
    if (res.close) {
      this._drop(ws);
      try { ws.close(res.close, ''); } catch {}
      return;
    }
    for (const tid of res.to) {
      const target = this.sockets.get(tid);
      if (target) {
        try { target.send(message); } catch {}
      }
    }
  }

  async webSocketClose(ws) {
    this._drop(ws);
  }

  async webSocketError(ws) {
    this._drop(ws);
    try { ws.close(1011, ''); } catch {}
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/v' || url.pathname === '/t' || url.pathname.startsWith('/mb/')) {
      return mailbox(request, env, url);
    }
    if (!PATH_RE.test(url.pathname)) {
      return new Response('Not found', { status: 404 });
    }
    if ((request.headers.get('Upgrade') || '').toLowerCase() !== 'websocket') {
      return new Response('Upgrade Required', { status: 426 });
    }
    if (!originAllowed(request.headers.get('Origin') || '')) {
      return new Response('Forbidden', { status: 403 });
    }
    const stub = env.HUB.get(env.HUB.idFromName('hub'));
    return stub.fetch(request);
  },
};
