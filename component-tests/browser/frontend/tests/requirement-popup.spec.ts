import type { Page } from '@playwright/test';
import { test, expect, fixture, reveal, expectFullyVisible, expectInViewport, settle } from './support';

const ENTRY = '/?mode=requirements&session_id=requirements-session-7d3e9a1c5b8f4e2a9d6c0b1f3e5a7c9d';

async function openInterview(page: Page): Promise<void> {
  await fixture(page, 'getAuthMe');
  await fixture(page, 'getWorkflows');
  await fixture(page, 'getRequirementsSessionsBySessionId');
  await page.goto(ENTRY);
  await expect(page.getByRole('region', { name: 'Your answer', exact: true })).toBeVisible();
}

/** Reveal the popup of the requirement ID `id` in the left column and check it whole. */
async function popupIsWhole(page: Page, id: string): Promise<void> {
  const button = page.getByRole('button', { name: id, exact: true });
  await reveal(button);
  const popup = page.getByRole('tooltip');
  await expect(popup).toBeVisible();
  await expect(popup).toContainText(id);
  await settle(page);
  await expectFullyVisible(popup);
  await expectInViewport(popup);
}

test.describe('requirement popup of a question in the left column', () => {
  test('hovering an ID in a question shows its whole popup above the history',
    { tag: ['@desktop'] }, async ({ page }) => {
      // requirement: FR-171
      await openInterview(page);
      await popupIsWhole(page, 'FR-117');
    });

  test('hovering an ID in an option shows its whole popup above the history',
    { tag: ['@desktop'] }, async ({ page }) => {
      // requirement: FR-171
      await openInterview(page);
      await popupIsWhole(page, 'FR-118');
    });
});

test.describe('requirement popup stays inside the window', () => {
  test('a popup low in the desktop window moves up and stays on top',
    { tag: ['@desktop'] }, async ({ page }) => {
      // requirement: FR-172
      await openInterview(page);
      await popupIsWhole(page, 'FR-121');
    });

  test('a popup at the right edge of the phone window moves left',
    { tag: ['@phone'] }, async ({ page }) => {
      // requirement: FR-172
      await openInterview(page);
      await popupIsWhole(page, 'FR-122');
    });
});

test.describe('whole popup in every window', () => {
  test('hovering FR-117 on desktop shows the whole popup',
    { tag: ['@desktop'] }, async ({ page }) => {
      // requirement: FR-173
      await openInterview(page);
      await popupIsWhole(page, 'FR-117');
    });

  test('hovering FR-118 on desktop shows the whole popup',
    { tag: ['@desktop'] }, async ({ page }) => {
      // requirement: FR-173
      await openInterview(page);
      await popupIsWhole(page, 'FR-118');
    });

  test('hovering FR-121 on desktop shows the whole popup',
    { tag: ['@desktop'] }, async ({ page }) => {
      // requirement: FR-173
      await openInterview(page);
      await popupIsWhole(page, 'FR-121');
    });

  test('hovering FR-122 on desktop shows the whole popup',
    { tag: ['@desktop'] }, async ({ page }) => {
      // requirement: FR-173
      await openInterview(page);
      await popupIsWhole(page, 'FR-122');
    });

  test('tapping FR-117 on tablet and phone shows the whole popup',
    { tag: ['@tablet', '@phone'] }, async ({ page }) => {
      // requirement: FR-173
      await openInterview(page);
      await popupIsWhole(page, 'FR-117');
    });

  test('tapping FR-118 on tablet and phone shows the whole popup',
    { tag: ['@tablet', '@phone'] }, async ({ page }) => {
      // requirement: FR-173
      await openInterview(page);
      await popupIsWhole(page, 'FR-118');
    });

  test('tapping FR-121 on tablet and phone shows the whole popup',
    { tag: ['@tablet', '@phone'] }, async ({ page }) => {
      // requirement: FR-173
      await openInterview(page);
      await popupIsWhole(page, 'FR-121');
    });

  test('tapping FR-122 on tablet and phone shows the whole popup',
    { tag: ['@tablet', '@phone'] }, async ({ page }) => {
      // requirement: FR-173
      await openInterview(page);
      await popupIsWhole(page, 'FR-122');
    });
});
