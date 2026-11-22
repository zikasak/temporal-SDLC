// Trusted conformance check of a page contract: opens each page of pages.json at each
// viewport of viewports.json in headless Chromium, against the part's built web client,
// with every API call answered from the contract's fixtures, and fails when a declared
// element cannot be found once the page is idle, when an element the change is to add is
// there already, or when the page calls an operation the contract does not declare or
// cannot answer.
//
//   node check-pages.mjs --contract DIR --site DIR [--operations FILE] [--absent FILE]
//
// --site is the part's `vite build` output; --operations the API's operations
// ([{operationId, method, path}]); --absent the elements the change adds, which must not
// be found yet. Run it from a directory where @playwright/test is installed, with its
// Chromium.
import { createReadStream, existsSync, readFileSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { extname, join, normalize, resolve, sep } from 'node:path';

function option(name, fallback) {
  const index = process.argv.indexOf(`--${name}`);
  if (index < 0) return fallback;
  return process.argv[index + 1];
}

const contract = resolve(option('contract', '/contract'));
const site = resolve(option('site', '/site'));
const operationsFile = option('operations', join(contract, 'operations.json'));
const absentFile = option('absent');
const require = createRequire(join(process.cwd(), 'noop.js'));
const { chromium } = require('@playwright/test');

const readJson = (file) => JSON.parse(readFileSync(file, 'utf8'));
const pages = readJson(join(contract, 'pages.json'));
const viewports = readJson(join(contract, 'viewports.json'));
const operations = existsSync(operationsFile) ? readJson(operationsFile) : [];
const absent = new Set((absentFile && existsSync(absentFile) ? readJson(absentFile) : []).map(key));
const prefix = (pages.api_prefix || '').replace(/\/+$/, '');

function escape(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

const matchers = operations.map((operation) => ({
  ...operation,
  pattern: new RegExp(`^${operation.path.split(/(\{[^}/]+\})/).map((segment) => (
    /^\{[^}/]+\}$/.test(segment) ? '[^/]+' : escape(segment))).join('')}$`),
  literal: operation.path.replace(/\{[^}/]+\}/g, '').length,
})).sort((a, b) => b.literal - a.literal);

function isApi(pathname) {
  return prefix ? pathname === prefix || pathname.startsWith(`${prefix}/`)
    : matchers.some((operation) => operation.pattern.test(pathname));
}

function operationOf(method, pathname) {
  const path = pathname.slice(prefix.length) || '/';
  const found = matchers.find((operation) => operation.method === method && operation.pattern.test(path));
  return found ? found.operationId : `${method} ${pathname}`;
}

/** How an element of the contract is named in messages and in --absent. */
function key(element) {
  if (element.testid !== undefined) return `testid=${element.testid}`;
  return element.name !== undefined ? `role=${element.role}[name=${JSON.stringify(element.name)}]`
    : `role=${element.role}`;
}

function locate(page, element) {
  if (element.testid !== undefined) return page.getByTestId(element.testid);
  return page.getByRole(element.role, element.name !== undefined
    ? { name: element.name, exact: true } : {});
}

function serve() {
  const server = createServer((request, response) => {
    const { pathname } = new URL(request.url || '/', 'http://localhost');
    let path = null;
    try {
      const candidate = normalize(join(site, decodeURIComponent(pathname)));
      if ((candidate === site || candidate.startsWith(site + sep)) && statSync(candidate).isFile()) {
        path = candidate;
      }
    } catch {
      path = null;
    }
    path ??= extname(pathname) ? null : join(site, 'index.html');
    if (!path) {
      response.writeHead(404).end();
      return;
    }
    const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
      '.svg': 'image/svg+xml', '.json': 'application/json', '.woff2': 'font/woff2' };
    response.writeHead(200, { 'Content-Type': types[extname(path)] || 'application/octet-stream' });
    createReadStream(path).pipe(response);
  });
  return new Promise((done) => server.listen(0, '127.0.0.1', () => done(server)));
}

async function present(page, element, timeout) {
  try {
    await locate(page, element).first().waitFor({ state: 'attached', timeout });
    return true;
  } catch {
    return false;
  }
}

const errors = [];

async function open(context, base, item, viewportName, action) {
  const where = `Page ${item.entry} at ${viewportName}${action ? ` after ${action.input} on ${key(action.target)}` : ''}`;
  const declared = new Set([...(item.operations || []), ...(action?.operations || [])]);
  const page = await context.newPage();
  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== base) {
      await route.abort('blockedbyclient');
      return;
    }
    if (!isApi(url.pathname)) {
      await route.fallback();
      return;
    }
    const operation = operationOf(request.method(), url.pathname);
    if (!declared.has(operation)) {
      errors.push(`${where}: calls ${operation}, which the page contract does not declare`);
      await route.abort('failed');
      return;
    }
    const file = join(contract, 'fixtures', `${operation}.json`);
    if (!existsSync(file)) {
      errors.push(`${where}: calls ${operation}, which no fixture fixtures/${operation}.json answers`);
      await route.abort('failed');
      return;
    }
    const answer = readJson(file);
    await route.fulfill({ status: answer.status, contentType: 'application/json',
      body: answer.body === null || answer.body === undefined ? '' : JSON.stringify(answer.body) });
  });
  try {
    await page.goto(new URL(item.entry, base).toString(), { waitUntil: 'load', timeout: 30_000 });
    await page.waitForLoadState('networkidle', { timeout: 10_000 }).catch(() => undefined);
    await page.evaluate(() => document.fonts.ready.then(() => undefined));
    return { page, where };
  } catch (error) {
    errors.push(`${where}: does not load: ${String(error.message || error).split('\n')[0]}`);
    await page.close();
    return null;
  }
}

async function check(context, base, item, viewportName, touch) {
  const opened = await open(context, base, item, viewportName, null);
  if (opened) {
    for (const element of item.elements || []) {
      const added = absent.has(key(element));
      const found = await present(opened.page, element, added ? 1_000 : 5_000);
      if (!added && !found) errors.push(`${opened.where}: ${key(element)} is not present`);
      if (added && found) errors.push(`${opened.where}: ${key(element)} is present already, so the change does not add it`);
    }
    await opened.page.close();
  }
  for (const action of item.actions || []) {
    if (absent.has(key(action.target))) continue; // its target is added by the change
    const after = await open(context, base, item, viewportName, action);
    if (!after) continue;
    const target = locate(after.page, action.target).first();
    try {
      const input = action.input === 'reveal' ? (touch ? 'tap' : 'hover') : action.input;
      if (input === 'hover') await target.hover({ timeout: 5_000 });
      else if (input === 'tap') await target.tap({ timeout: 5_000 });
      else if (input === 'click') await target.click({ timeout: 5_000 });
      else await target.focus({ timeout: 5_000 });
    } catch {
      errors.push(`${after.where}: ${key(action.target)} is not present or cannot take ${action.input}`);
      await after.page.close();
      continue;
    }
    await after.page.waitForLoadState('networkidle', { timeout: 5_000 }).catch(() => undefined);
    for (const element of action.shows || []) {
      const added = absent.has(key(element));
      const found = await present(after.page, element, added ? 1_000 : 5_000);
      if (!added && !found) errors.push(`${after.where}: ${key(element)} is not present`);
      if (added && found) errors.push(`${after.where}: ${key(element)} is present already, so the change does not add it`);
    }
    await after.page.close();
  }
}

const server = await serve();
const base = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ args: ['--disable-dev-shm-usage'] });
try {
  for (const [name, viewport] of Object.entries(viewports)) {
    const touch = viewport.input === 'touch';
    const context = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
      deviceScaleFactor: viewport.scale ?? 1, hasTouch: touch, isMobile: touch,
      reducedMotion: 'reduce', serviceWorkers: 'block', locale: 'en-US', timezoneId: 'UTC',
    });
    for (const item of pages.pages || []) await check(context, base, item, name, touch);
    await context.close();
  }
} finally {
  await browser.close();
  server.close();
}
if (errors.length > 0) {
  for (const error of [...new Set(errors)]) console.error(error);
  process.exit(1);
}
console.log('The page contract conforms');
