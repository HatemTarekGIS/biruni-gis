import http from 'node:http';
import worker from '../relay/worker.js';
import { FakeR2 } from './mb_fake_r2.mjs';

const ALLOWED_PHOTO_ORIGIN = 'https://photos.biruni-gis.com';
const LOCAL_HTTP_RE = /^http:\/\/127\.0\.0\.1:\d+$/;

export function startMbServer({ secret = process.env.MB_SECRET, withBucket = true } = {}) {
  const r2 = withBucket ? new FakeR2() : null;
  const env = {};
  if (r2) env.MB = r2;
  if (secret) env.MB_SECRET = secret;

  const server = http.createServer(async (req, res) => {
    try {
      if (req.method === 'GET' && req.url === '/__keys') {
        const keys = r2 ? [...r2.map.keys()].sort() : [];
        const payload = JSON.stringify({ keys });
        res.writeHead(200, {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(payload),
        });
        res.end(payload);
        return;
      }

      const chunks = [];
      for await (const chunk of req) {
        chunks.push(chunk);
      }
      const bodyBuf = Buffer.concat(chunks);

      const headers = new Headers();
      let mappedFrom = null;
      for (const [k, v] of Object.entries(req.headers)) {
        if (v === undefined) continue;
        const val = Array.isArray(v) ? v.join(', ') : String(v);
        if (k.toLowerCase() === 'origin' && LOCAL_HTTP_RE.test(val)) {
          mappedFrom = val;
          headers.set('Origin', ALLOWED_PHOTO_ORIGIN);
        } else {
          headers.set(k, val);
        }
      }

      const fullUrl = `https://biruni-relay.test.workers.dev${req.url}`;
      const method = (req.method || 'GET').toUpperCase();
      const hasBody = method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS';
      const wReq = new Request(fullUrl, {
        method,
        headers,
        body: hasBody ? bodyBuf : undefined,
      });

      const wRes = await worker.fetch(wReq, env);
      const outHeaders = {};
      wRes.headers.forEach((v, k) => {
        if (k.toLowerCase() === 'access-control-allow-origin' && mappedFrom && v === ALLOWED_PHOTO_ORIGIN) {
          outHeaders[k] = mappedFrom;
        } else {
          outHeaders[k] = v;
        }
      });

      const outBuf = Buffer.from(await wRes.arrayBuffer());
      outHeaders['content-length'] = String(outBuf.length);
      res.writeHead(wRes.status, outHeaders);
      res.end(outBuf);
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'text/plain' });
      res.end(String(err));
    }
  });

  return { server, r2, env };
}

if (import.meta.url === `file:///${process.argv[1].replace(/\\/g, '/')}` || process.argv[1]?.endsWith('mb_node_server.mjs')) {
  const { server } = startMbServer();
  server.listen(0, '127.0.0.1', () => {
    const addr = server.address();
    console.log(`PORT ${addr.port}`);
  });
}
