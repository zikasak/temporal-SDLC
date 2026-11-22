// Trusted configuration of the runner. One Playwright project per viewport of the page
// contract's viewports.json (CONTRACT), each running only the tests tagged with its name
// (`{ tag: ['@narrow'] }`), in headless Chromium against the part's built client (SITE),
// served on 127.0.0.1. REPORT is where the JUnit report is written, and CLASSNAME_PREFIX,
// when set, what starts every class name of it. It imports nothing but Node's own modules,
// so it loads from outside the suite.
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const runner = dirname(fileURLToPath(import.meta.url));
const contract = process.env.CONTRACT || '/contract';
const port = Number(process.env.PORT || 4173);
const baseURL = `http://127.0.0.1:${port}`;
const viewports = JSON.parse(readFileSync(join(contract, 'viewports.json'), 'utf8'));

function escape(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

export default {
  testDir: resolve(process.cwd(), 'tests'),
  testMatch: '**/*.spec.ts',
  testIgnore: '**/support/**',
  outputDir: process.env.RESULTS || '/tmp/playwright-results',
  forbidOnly: true,
  retries: 0,
  workers: 2,
  timeout: 30_000,
  expect: { timeout: 5_000 },
  reporter: [['list'], [join(runner, 'reporter.mjs'), {
    outputFile: process.env.REPORT || 'reports/junit.xml',
    prefix: process.env.CLASSNAME_PREFIX || '',
  }]],
  webServer: {
    command: `node ${JSON.stringify(join(runner, 'serve.mjs'))}`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 30_000,
    env: { SITE: process.env.SITE || '/site', PORT: String(port) },
  },
  use: {
    browserName: 'chromium',
    baseURL,
    locale: 'en-US',
    timezoneId: 'UTC',
    colorScheme: 'light',
    reducedMotion: 'reduce',
    serviceWorkers: 'block',
    deviceScaleFactor: 1,
    trace: 'off',
    screenshot: 'off',
    video: 'off',
    launchOptions: { args: ['--disable-dev-shm-usage', '--font-render-hinting=none'] },
  },
  projects: Object.entries(viewports).map(([name, viewport]) => {
    const touch = viewport.input === 'touch';
    return {
      name,
      // The tag, not a word of a title: names hold no `@`.
      grep: new RegExp(`@${escape(name)}(?![\\w-])`),
      use: {
        viewport: { width: viewport.width, height: viewport.height },
        deviceScaleFactor: viewport.scale ?? 1,
        hasTouch: touch,
        isMobile: touch,
      },
    };
  }),
};
