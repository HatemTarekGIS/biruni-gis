import crypto from 'node:crypto';

async function toBuffer(body) {
  if (body === null || body === undefined) return Buffer.alloc(0);
  if (Buffer.isBuffer(body)) return Buffer.from(body);
  if (body instanceof ArrayBuffer) return Buffer.from(body);
  if (ArrayBuffer.isView(body)) return Buffer.from(body.buffer, body.byteOffset, body.byteLength);
  if (typeof body === 'string') return Buffer.from(body, 'utf8');
  if (typeof body.getReader === 'function') {
    const reader = body.getReader();
    const chunks = [];
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(Buffer.from(value));
    }
    return Buffer.concat(chunks);
  }
  if (Symbol.asyncIterator in Object(body)) {
    const chunks = [];
    for await (const chunk of body) {
      chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    }
    return Buffer.concat(chunks);
  }
  return Buffer.from(body);
}

function normShaHex(sha256) {
  if (!sha256) return null;
  if (typeof sha256 === 'string') return sha256.toLowerCase();
  if (sha256 instanceof ArrayBuffer) return Buffer.from(sha256).toString('hex');
  if (ArrayBuffer.isView(sha256)) {
    return Buffer.from(sha256.buffer, sha256.byteOffset, sha256.byteLength).toString('hex');
  }
  return String(sha256).toLowerCase();
}

function hasIfNoneMatchStar(onlyIf) {
  if (!onlyIf) return false;
  if (typeof onlyIf.get === 'function') {
    return onlyIf.get('If-None-Match') === '*' || onlyIf.get('if-none-match') === '*';
  }
  if (typeof onlyIf === 'object') {
    return (
      onlyIf['If-None-Match'] === '*' ||
      onlyIf['if-none-match'] === '*' ||
      onlyIf.etagDoesNotMatch === '*'
    );
  }
  return false;
}

export class FakeR2 {
  constructor() {
    this.map = new Map();
  }

  async head(key) {
    const item = this.map.get(key);
    if (!item) return null;
    return {
      key,
      size: item.buf.length,
      uploaded: item.uploaded,
      customMetadata: { ...item.customMetadata },
    };
  }

  async get(key) {
    const item = this.map.get(key);
    if (!item) return null;
    const buf = Buffer.from(item.buf);
    return {
      key,
      size: buf.length,
      uploaded: item.uploaded,
      customMetadata: { ...item.customMetadata },
      body: new ReadableStream({
        start(controller) {
          controller.enqueue(new Uint8Array(buf));
          controller.close();
        },
      }),
      async arrayBuffer() {
        return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
      },
      async text() {
        return buf.toString('utf8');
      },
    };
  }

  async put(key, body, opts = {}) {
    const buf = await toBuffer(body);
    const expectedSha = normShaHex(opts.sha256);
    if (expectedSha) {
      const actualSha = crypto.createHash('sha256').update(buf).digest('hex');
      if (actualSha !== expectedSha) {
        throw new Error('R2 checksum mismatch');
      }
    }
    if (hasIfNoneMatchStar(opts.onlyIf) && this.map.has(key)) {
      return null;
    }
    const entry = {
      buf,
      uploaded: new Date(),
      customMetadata: opts.customMetadata ? { ...opts.customMetadata } : {},
    };
    this.map.set(key, entry);
    return {
      key,
      size: buf.length,
      uploaded: entry.uploaded,
      customMetadata: { ...entry.customMetadata },
    };
  }

  async list({ prefix = '', cursor, limit = 500, include = [] } = {}) {
    const keys = [...this.map.keys()].filter((k) => k.startsWith(prefix)).sort();
    const start = cursor ? Number(cursor) || 0 : 0;
    const lim = Number(limit) > 0 ? Number(limit) : 500;
    const slice = keys.slice(start, start + lim);
    const incMeta = Array.isArray(include) && include.includes('customMetadata');
    const objects = slice.map((k) => {
      const item = this.map.get(k);
      const obj = {
        key: k,
        size: item.buf.length,
        uploaded: item.uploaded,
      };
      if (incMeta) {
        obj.customMetadata = { ...item.customMetadata };
      }
      return obj;
    });
    const nextIdx = start + lim;
    const truncated = nextIdx < keys.length;
    return {
      objects,
      truncated,
      cursor: truncated ? String(nextIdx) : '',
    };
  }
}
