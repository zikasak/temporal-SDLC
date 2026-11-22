// Trusted static server of the runner: the part's built web client (SITE) on 127.0.0.1
// (PORT), with every path that is not a file answered by index.html, as a single-page
// client is served. Requests to the API never reach it: the tests answer them from the
// page contract's fixtures.
import { createReadStream, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { extname, join, normalize, resolve, sep } from 'node:path';

const site = resolve(process.env.SITE || '/site');
const port = Number(process.env.PORT || 4173);
const types = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp',
  '.ico': 'image/x-icon', '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf',
  '.txt': 'text/plain; charset=utf-8', '.map': 'application/json', '.wasm': 'application/wasm',
};

function file(pathname) {
  let decoded;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    return null;
  }
  const path = normalize(join(site, decoded));
  if (path !== site && !path.startsWith(site + sep)) return null;
  try {
    return statSync(path).isFile() ? path : null;
  } catch {
    return null;
  }
}

createServer((request, response) => {
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    response.writeHead(405).end();
    return;
  }
  const { pathname } = new URL(request.url || '/', 'http://localhost');
  // A file of the build, or the client's own page for any route of it.
  const path = file(pathname) ?? (extname(pathname) ? null : join(site, 'index.html'));
  if (!path) {
    response.writeHead(404).end();
    return;
  }
  response.writeHead(200, {
    'Content-Type': types[extname(path).toLowerCase()] || 'application/octet-stream',
    'Cache-Control': 'no-store',
  });
  if (request.method === 'HEAD') {
    response.end();
    return;
  }
  createReadStream(path).pipe(response);
}).listen(port, '127.0.0.1');
