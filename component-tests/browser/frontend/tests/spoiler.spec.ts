import type { Page } from '@playwright/test';
import { test, expect, fixture, settle, expectFullyVisible } from './support';

const ENTRY = '/?mode=requirements&session_id=requirements-session-0123456789abcdef0123456789abcdef';

function toggle(page: Page) {
  return page.getByRole('heading', { name: 'Requirements gathered (2)', exact: true });
}

function requirements(page: Page) {
  return page.getByRole('region', { name: 'Requirements gathered', exact: true });
}

async function openInterview(page: Page): Promise<void> {
  await fixture(page, 'getAuthMe');
  await fixture(page, 'getWorkflows');
  await fixture(page, 'getRequirementsSessionsBySessionId');
  await page.goto(ENTRY);
  await expect(toggle(page)).toBeVisible();
}

test.describe('spoiler toggle of the requirements section on the requirements page', () => {
  test('the section stays hidden until its heading is clicked, then re-hidden',
    { tag: ['@desktop', '@phone'] }, async ({ page }) => {
      // requirement: FR-221
      await openInterview(page);
      // The spoiler always starts closed when the interview is opened.
      await expect(requirements(page)).toBeHidden();
      // The heading stays visible as the toggle while the content is hidden.
      await expect(toggle(page)).toBeVisible();
      await toggle(page).click();
      await expect(requirements(page)).toBeVisible();
      await expect(toggle(page)).toBeVisible();
      await toggle(page).click();
      await expect(requirements(page)).toBeHidden();
    });

  test('the gathered requirements are not rendered openly at first',
    { tag: ['@desktop', '@phone'] }, async ({ page }) => {
      // requirement: FR-222
      await openInterview(page);
      // The results are hidden below the heading until the toggle is clicked.
      await expect(requirements(page)).toBeHidden();
      await toggle(page).click();
      await expect(requirements(page)).toBeVisible();
      await toggle(page).click();
      await expect(requirements(page)).toBeHidden();
    });

  test('the toggle stays inside the window and opens and closes the content',
    { tag: ['@desktop', '@phone'] }, async ({ page }) => {
      // requirement: FR-223
      await openInterview(page);
      await settle(page);
      // The section heading stays inside the visible window, not cut off.
      await expectFullyVisible(toggle(page));
      await expect(requirements(page)).toBeHidden();
      await toggle(page).click();
      await expect(requirements(page)).toBeVisible();
      await toggle(page).click();
      await expect(requirements(page)).toBeHidden();
    });
});
