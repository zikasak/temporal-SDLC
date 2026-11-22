// Trusted helpers of the browser tests, supplied by the runner: the test author imports
// them and never writes or changes them. Every request the page sends to the API is
// answered from the page contract's fixtures, or fails the test naming its operation, and
// the layout assertions are written once here, so a reviewer relies on them instead of
// reading geometry code in each test.
import { test as base, expect, type BrowserContext, type Locator, type Page } from '@playwright/test';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

export { expect };

/** A response of the page contract: `fixtures/<operationId>[.<case>].json`. */
type Fixture = { status: number; body: unknown };
type Operation = { operationId: string; method: string; path: string };
type Matcher = Operation & { pattern: RegExp; literal: number };

// The page contract as the runner holds it: pages.json, viewports.json, fixtures/, and
// operations.json, the API's operations, which the host reads from contracts/openapi.yaml.
const contract = process.env.CONTRACT || '/contract';

function readJson(file: string): unknown {
  return JSON.parse(readFileSync(file, 'utf8'));
}

function apiPrefix(): string {
  const pages = readJson(join(contract, 'pages.json')) as { api_prefix?: unknown };
  return typeof pages.api_prefix === 'string' ? pages.api_prefix.replace(/\/+$/, '') : '';
}

function escape(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function matchers(): Matcher[] {
  const file = join(contract, 'operations.json');
  const operations = existsSync(file) ? readJson(file) as Operation[] : [];
  return operations.map((operation) => ({
    ...operation,
    pattern: new RegExp(`^${operation.path.split(/(\{[^}/]+\})/).map((segment) => (
      /^\{[^}/]+\}$/.test(segment) ? '[^/]+' : escape(segment))).join('')}$`),
    literal: operation.path.replace(/\{[^}/]+\}/g, '').length,
  // The most literal path first: /items/new before /items/{id}.
  })).sort((a, b) => b.literal - a.literal);
}

let cache: { prefix: string; operations: Matcher[] } | null = null;

function api() {
  cache ??= { prefix: apiPrefix(), operations: matchers() };
  return cache;
}

/** Whether a path of the page's own server is the API's: below the contract's
 * `api_prefix`, or, without one, a path of one of its operations. */
function isApi(pathname: string): boolean {
  const { prefix, operations } = api();
  return prefix ? pathname === prefix || pathname.startsWith(`${prefix}/`)
    : operations.some((operation) => operation.pattern.test(pathname));
}

/** The operation a request of the page to the API is, by method and path, or the
 * request itself when the API has no such operation. */
function operationOf(method: string, pathname: string): string {
  const { prefix, operations } = api();
  const path = pathname.slice(prefix.length) || '/';
  const found = operations.find((operation) => (
    operation.method === method && operation.pattern.test(path)));
  return found ? found.operationId : `${method} ${pathname}`;
}

// The answers of a test, kept by browser context: requests are intercepted for the whole
// context, so a pop-up or a second page of the test is answered as its first page is.
const answers = new WeakMap<BrowserContext, Map<string, Fixture>>();

function answersOf(context: BrowserContext): Map<string, Fixture> {
  let map = answers.get(context);
  if (!map) {
    map = new Map();
    answers.set(context, map);
  }
  return map;
}

const OPERATION = /^[A-Za-z0-9_.-]{1,120}$/;
const CASE = /^[A-Za-z0-9_-]{1,60}$/;

/** Answer every call of `operationId` the test's pages make from now on with the
 * contract's fixture `fixtures/<operationId>.json`, or `fixtures/<operationId>.<name>.json`
 * with a `name`. Call it before the page sends the request, such as before `page.goto`. */
export async function fixture(page: Page, operationId: string, name?: string): Promise<void> {
  if (!OPERATION.test(operationId) || operationId.includes('..')
      || (name !== undefined && !CASE.test(name))) {
    throw new Error(`No fixture is named ${operationId}${name ? `.${name}` : ''}`);
  }
  const file = join(contract, 'fixtures', name ? `${operationId}.${name}.json` : `${operationId}.json`);
  if (!existsSync(file)) {
    throw new Error(`The page contract has no fixture ${name ? `${operationId}.${name}` : operationId}`);
  }
  answersOf(page.context()).set(operationId, readJson(file) as Fixture);
}

/** The test of the browser tests: every request to the API is answered from the fixtures
 * the test chose, any other fails the test naming its operation, and nothing outside the
 * page's own server is reached. */
export const test = base.extend<{ api: void }>({
  api: [async ({ page, baseURL }, use) => {
    const origin = new URL(baseURL || 'http://127.0.0.1').origin;
    const unanswered: string[] = [];
    await page.context().route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin !== origin) {
        // No network: what the page loads from elsewhere, such as a web font, is not there.
        await route.abort('blockedbyclient');
        return;
      }
      if (!isApi(url.pathname)) {
        // The page's own files, from the runner's static server.
        await route.fallback();
        return;
      }
      const operation = operationOf(request.method(), url.pathname);
      const answer = answersOf(page.context()).get(operation);
      if (!answer) {
        unanswered.push(operation);
        await route.abort('failed');
        return;
      }
      await route.fulfill({
        status: answer.status,
        contentType: 'application/json',
        body: answer.body === null || answer.body === undefined ? '' : JSON.stringify(answer.body),
      });
    });
    await use();
    if (unanswered.length > 0) {
      throw new Error('The page called operations the test did not answer with a fixture: '
        + [...new Set(unanswered)].join(', '));
    }
  }, { auto: true }],
});

type Box = { left: number; top: number; right: number; bottom: number };

function describeBox(box: Box): string {
  const round = (value: number) => Math.round(value * 10) / 10;
  return `(${round(box.left)}, ${round(box.top)})–(${round(box.right)}, ${round(box.bottom)})`;
}

/** The element of `locator` lies wholly inside the window. */
export async function expectInViewport(locator: Locator): Promise<void> {
  await expect(locator).toBeVisible();
  await expect(async () => {
    const { box, width, height } = await locator.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return {
        box: { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom },
        width: window.innerWidth, height: window.innerHeight,
      };
    });
    const tolerance = 0.5;
    expect(box.left >= -tolerance && box.top >= -tolerance && box.right <= width + tolerance
      && box.bottom <= height + tolerance,
    `${locator} at ${describeBox(box)} is not inside the window of ${width}×${height}`).toBe(true);
  }).toPass({ timeout: 5000 });
}

/** The element of `locator` is inside the window and on top: at its four corners, inset
 * by 2 pixels, and at its centre, the element the page shows is it or one inside it. An
 * element cut off by a scrolling or clipping ancestor, or covered by another, fails. As
 * `document.elementFromPoint` does, it does not see an element with `pointer-events:
 * none`, so such an overlay over the element is not found. */
export async function expectFullyVisible(locator: Locator): Promise<void> {
  await expectInViewport(locator);
  await expect(async () => {
    const points = await locator.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const inset = Math.min(2, rect.width / 4, rect.height / 4);
      const spots: [string, number, number][] = [
        ['top left', rect.left + inset, rect.top + inset],
        ['top right', rect.right - inset, rect.top + inset],
        ['bottom left', rect.left + inset, rect.bottom - inset],
        ['bottom right', rect.right - inset, rect.bottom - inset],
        ['centre', rect.left + rect.width / 2, rect.top + rect.height / 2],
      ];
      const describe = (node: Element | null) => {
        if (!node) return 'nothing';
        const classes = typeof node.className === 'string' && node.className
          ? `.${node.className.trim().split(/\s+/).join('.')}` : '';
        return `${node.tagName.toLowerCase()}${node.id ? `#${node.id}` : ''}${classes}`;
      };
      return spots.map(([where, x, y]) => {
        const hit = document.elementFromPoint(x, y);
        return { where, x: Math.round(x), y: Math.round(y),
          covered: !hit || (hit !== element && !element.contains(hit)), by: describe(hit) };
      });
    });
    const hidden = points.filter((point) => point.covered)
      .map((point) => `${point.where} (${point.x}, ${point.y}) shows ${point.by}`);
    expect(hidden, `${locator} is cut off or covered: ${hidden.join('; ')}`).toEqual([]);
  }).toPass({ timeout: 5000 });
}

/** Show what `locator` reveals the way the window's input does: hover with a mouse, tap
 * on a touch screen. One test then covers "by mouse and by tap" in both viewports. */
export async function reveal(locator: Locator): Promise<void> {
  if (test.info().project.use.hasTouch) {
    await locator.tap();
  } else {
    await locator.hover();
  }
}

/** Wait until the page is still: its fonts loaded and every animation or transition that
 * ends has ended. One that repeats forever, such as a spinner, is not waited for, and no
 * wait lasts longer than 5 seconds. */
export async function settle(page: Page): Promise<void> {
  await page.evaluate(async () => {
    const still = (async () => {
      await document.fonts.ready;
      await Promise.all(document.getAnimations()
        .filter((animation) => animation.effect?.getTiming().iterations !== Infinity)
        .map((animation) => animation.finished.catch(() => undefined)));
    })();
    await Promise.race([still, new Promise((resolve) => setTimeout(resolve, 5000))]);
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
}
