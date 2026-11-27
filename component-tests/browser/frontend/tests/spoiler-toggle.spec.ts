import type { Page } from '@playwright/test';
import { test, expect, fixture, expectFullyVisible, settle } from './support';

const ENTRY = '/?mode=requirements&session_id=requirements-session-7d3e9a1c5b8f4e2a9d6c0b1f3e5a7c9d';

async function openInterview(page: Page): Promise<void> {
  await fixture(page, 'getAuthMe');
  await fixture(page, 'getWorkflows');
  await fixture(page, 'getRequirementsSessionsBySessionId');
  await page.goto(ENTRY);
  await expect(page.getByRole('region', { name: 'Your answer', exact: true })).toBeVisible();
}

function toggle(page: Page) {
  return page.getByRole('heading', { name: 'Requirements gathered (0)', exact: true });
}

function content(page: Page) {
  return page.getByRole('heading', { name: 'Functional', exact: true });
}

test.describe('requirements interview spoiler', () => {
  test('the interview stays open with its gathered requirements hidden under the heading',
    { tag: ['@desktop', '@tablet', '@phone'] }, async ({ page }) => {
      // requirement: FR-221
      await openInterview(page);
      await expect(toggle(page)).toBeVisible();
      await expect(content(page)).toBeHidden();
      await toggle(page).click();
      await expect(content(page)).toBeVisible();
      await toggle(page).click();
      await expect(content(page)).toBeHidden();
      await expect(page.getByRole('region', { name: 'Your answer', exact: true })).toBeVisible();
    });

  test('the gathered requirements start closed and re-hide on a second click',
    { tag: ['@desktop'] }, async ({ page }) => {
      // requirement: FR-222
      await openInterview(page);
      await expect(toggle(page)).toBeVisible();
      await expect(content(page)).toBeHidden();
      await toggle(page).click();
      await expect(content(page)).toBeVisible();
      await toggle(page).click();
      await expect(content(page)).toBeHidden();
    });

  test('the toggle heading stays inside the window and reveals the content on tap and second click',
    { tag: ['@desktop', '@phone'] }, async ({ page }) => {
      // requirement: FR-223
      await openInterview(page);
      await settle(page);
      await expectFullyVisible(toggle(page));
      await expect(content(page)).toBeHidden();
      await toggle(page).click();
      await expect(content(page)).toBeVisible();
      await toggle(page).click();
      await expect(content(page)).toBeHidden();
    });
});
