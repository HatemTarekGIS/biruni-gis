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

function originAllowed(origin) {
  if (!origin) return false;
  if (ALLOWED_ORIGINS.has(origin)) return true;
  const m = LOCAL_ORIGIN_RE.exec(origin);
  return !!m && Number(m[1]) <= 65535;
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
