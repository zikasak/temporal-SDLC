# Browser tests

This standalone Playwright project tests behaviour of a part of the repository that only
its laid-out page shows: whether an element is cut off, covered or inside the window, at
a window size, by mouse or by tap. The tests are written from the page contract in
`contracts/parts/<part>/browser/` of the repository alone: `pages.json` (the pages, the
elements they declare and the API operations they call), `fixtures/` (the responses the
tests answer those operations with, checked against `contracts/openapi.yaml`) and
`viewports.json` (the window sizes and inputs the tests run in).

Each test is tagged with the viewports it runs in, as string literals:

```ts
import { test, expect, fixture, reveal, expectFullyVisible } from './support';

test.describe('the requirement popup', () => {
  test('is fully visible', { tag: ['@desktop', '@narrow'] }, async ({ page }) => {
    await fixture(page, 'getSession');
    await page.goto('/');
    await reveal(page.getByRole('button', { name: 'FR-1', exact: true }));
    await expectFullyVisible(page.getByRole('tooltip'));
  });
});
```

`tests/support/` holds the trusted helpers, which the runner always supplies as they are
here: `fixture` answers an operation from the contract, `expectFullyVisible`,
`expectInViewport`, `reveal` (hover with a mouse, tap on a touch screen) and `settle`.
Every request to the API that a test did not answer fails it, naming the operation, and
nothing outside the page's own server is reached.

The trusted Docker runner installs the part from its lock file, builds it with
`vite build`, installs this project from its own lock file, collects the suite during the
build and, when started without a network, serves the build on 127.0.0.1 and runs
`playwright test` with one project per viewport. The run is limited to ten minutes, and
the JUnit XML report is written to `/tests/reports/junit.xml`, each test's class name
naming its file and viewport (`tests/popup.spec.ts#narrow`).

## Pinned versions

The two must agree, and a backend test fails when they do not:

- `@playwright/test` **1.65.1**, exact, in `package.json` and `package-lock.json`;
- the runner image `mcr.microsoft.com/playwright:v1.65.1-noble`, by its multi-arch
  digest, `PREPULLED_IMAGES` in `backend/app/images.py`, whose bundled Chromium is the
  one that package expects.

Upgrading is a deliberate change of both, followed by a stability run of the browser
tests.

To type-check the tests:

```sh
npm ci --ignore-scripts
npx tsc -p tsconfig.json
```
