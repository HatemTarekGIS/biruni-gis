/*
 * Biruni relay Worker (Cloudflare Workers + Durable Objects Hibernation API).
 * Free-tier note: one hub object serves all rooms; at the limit, connections fail,
 * the client backs off for 10 minutes, and the other levels carry on.
 * No logging of topics or IPs.
 */
import { Hub } from './hub.js';

const TOPIC_RE = /^\/r\/(biruni[0-9a-f]{40})$/;
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
    this.hub = new Hub();
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
    const m = TOPIC_RE.exec(new URL(request.url).pathname);
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
    if (!TOPIC_RE.test(url.pathname)) {
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
