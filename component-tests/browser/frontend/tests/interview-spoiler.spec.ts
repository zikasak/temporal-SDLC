import type { Page } from '@playwright/test';
import { test, expect, fixture, expectFullyVisible, settle } from './support';

const ENTRY = '/?mode=requirements&session_id=requirements-session-0123456789abcdef0123456789abcdef';
const HEADING = 'Requirements gathered (2)';
const REGION = 'Requirements gathered';

function heading(page: Page) {
  return page.getByRole('heading', { name: HEADING, exact: true });
}

function region(page: Page) {
  return page.getByRole('region', { name: REGION, exact: true });
}

async function openInterview(page: Page): Promise<void> {
  await fixture(page, 'getAuthMe');
  await fixture(page, 'getWorkflows');
  await fixture(page, 'getRequirementsSessionsBySessionId');
  await page.goto(ENTRY);
  await expect(heading(page)).toBeVisible();
}

test.describe('spoiler toggle in the requirements interview', () => {
  test('starts closed and toggles the list content', { tag: ['@desktop', '@phone'] }, async ({ page }) => {
    // requirement: FR-221
    await openInterview(page);
    await expect(region(page)).not.toBeVisible();
    await heading(page).click();
    await expect(region(page)).toBeVisible();
    await expect(heading(page)).toBeVisible();
    await heading(page).click();
    await expect(region(page)).not.toBeVisible();
  });

  test('the list content stays hidden until the heading is clicked', { tag: ['@desktop', '@phone'] }, async ({ page }) => {
    // requirement: FR-222
    await openInterview(page);
    await expect(region(page)).not.toBeVisible();
    await heading(page).click();
    await expect(region(page)).toBeVisible();
    await heading(page).click();
    await expect(region(page)).not.toBeVisible();
  });

  test('the heading stays inside the window and toggles the list content', { tag: ['@desktop', '@phone'] }, async ({ page }) => {
    // requirement: FR-223
    await openInterview(page);
    await expect(region(page)).not.toBeVisible();
    await settle(page);
    await expectFullyVisible(heading(page));
    await heading(page).click();
    await expect(region(page)).toBeVisible();
    await heading(page).click();
    await expect(region(page)).not.toBeVisible();
  });
});
