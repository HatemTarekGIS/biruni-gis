const TOPIC_RE = /^biruni[0-9a-f]{40}$/;
const FRAME_RE = /^[A-Za-z0-9_-]{24,90000}$/;
const MAX_ROOMS = 2000;
const MAX_ROOM_SOCKETS = 64;
const REPLAY_WINDOW_MS = 60000;
const REPLAY_MAX = 20;
const RATE_PER_SEC = 5;
const BURST = 20;
const MAX_FRAME_LEN = 90000;

export class Hub {
  constructor() {
    this.rooms = new Map();
    this.sockets = new Map();
  }

  _prune(now) {
    for (const [topic, room] of this.rooms) {
      room.msgs = room.msgs.filter((m) => now - m.ts <= REPLAY_WINDOW_MS);
      if (room.sockets.size === 0 && room.msgs.length === 0) {
        this.rooms.delete(topic);
      }
    }
  }

  join(id, topic, now = Date.now()) {
    if (typeof topic !== 'string' || !TOPIC_RE.test(topic)) {
      return { close: 1008 };
    }
    if (this.sockets.has(id)) {
      this.leave(id);
    }
    if (!this.rooms.has(topic) && this.rooms.size >= MAX_ROOMS) {
      this._prune(now);
      if (this.rooms.size >= MAX_ROOMS) {
        return { close: 1013 };
      }
    }
    let room = this.rooms.get(topic);
    if (!room) {
      room = { sockets: new Set(), msgs: [] };
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
    const elapsed = Math.max(0, now - sock.lastRefill);
    sock.tokens = Math.min(BURST, sock.tokens + (elapsed * RATE_PER_SEC) / 1000);
    sock.lastRefill = now;
    if (sock.tokens < 1) {
      return { close: 1013 };
    }
    sock.tokens -= 1;

    const room = this.rooms.get(sock.topic);
    if (!room) {
      return { close: 1008 };
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
      if (room.sockets.size === 0 && room.msgs.length === 0) {
        this.rooms.delete(sock.topic);
      }
    }
    return { to: [], replay: [] };
  }
}
