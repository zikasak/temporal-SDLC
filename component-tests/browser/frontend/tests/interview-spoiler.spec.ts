import type { Locator, Page } from '@playwright/test';
import { test, expect, fixture, expectFullyVisible, expectInViewport, settle } from './support';

const REQUIREMENTS_ENTRY = '/?mode=requirements&session_id=requirements-session-7d3e9a1c5b8f4e2a9d6c0b1f3e5a7c9d';
const ARCHITECTURE_ENTRY = '/?mode=architecture&session_id=architecture-session-1a2b3c4d5e6f40718293a4b5c6d7e8f9';

async function openRequirementsInterview(page: Page): Promise<void> {
  await fixture(page, 'getAuthMe');
  await fixture(page, 'getWorkflows');
  await fixture(page, 'getRequirementsSessionsBySessionId');
  await page.goto(REQUIREMENTS_ENTRY);
}

async function openArchitectureInterview(page: Page): Promise<void> {
  await fixture(page, 'getAuthMe');
  await fixture(page, 'getWorkflows');
  await fixture(page, 'getArchitectureSessionsBySessionId');
  await page.goto(ARCHITECTURE_ENTRY);
}

/** Press the toggle the way the window's input does: a mouse click, or a tap on a touch
 * screen (the spoiler is opened by a click, which hovering would not do). */
async function press(locator: Locator): Promise<void> {
  if (test.info().project.use.hasTouch) {
    await locator.tap();
  } else {
    await locator.click();
  }
}

/** The heading toggle is shown whole, the content starts closed, opens on the first press
 * and closes again on the second. */
async function spoilerToggles(page: Page, toggle: Locator, content: Locator): Promise<void> {
  await expect(toggle).toBeVisible();
  await toggle.scrollIntoViewIfNeeded();
  await settle(page);
  await expectFullyVisible(toggle);
  await expectInViewport(toggle);
  // Closed when the interview is opened.
  await expect(content).toBeHidden();

  await press(toggle);
  await expect(content).toBeVisible();

  await press(toggle);
  await expect(content).toBeHidden();
  await expect(toggle).toBeVisible();
}

test.describe('requirements interview spoiler', () => {
  test('the requirements heading is a toggle that opens and closes the hidden list',
    { tag: ['@desktop', '@phone'] }, async ({ page }) => {
      // requirement: FR-221
      await openRequirementsInterview(page);
      await spoilerToggles(page,
        page.getByTestId('requirements-spoiler-toggle'),
        page.getByTestId('requirements-spoiler-content'));
    });

  test('the requirements toggle stays inside the window and reveals and hides the content by click or tap',
    { tag: ['@desktop', '@phone'] }, async ({ page }) => {
      // requirement: FR-223
      await openRequirementsInterview(page);
      const toggle = page.getByTestId('requirements-spoiler-toggle');
      const content = page.getByTestId('requirements-spoiler-content');
      await spoilerToggles(page, toggle, content);
      // After closing, the toggle is still whole inside the window.
      await toggle.scrollIntoViewIfNeeded();
      await settle(page);
      await expectFullyVisible(toggle);
    });
});

test.describe('architecture interview spoiler', () => {
  test('the architecture results are hidden on opening until the heading is pressed',
    { tag: ['@desktop', '@phone', '@tablet'] }, async ({ page }) => {
      // requirement: FR-222
      await openArchitectureInterview(page);
      await spoilerToggles(page,
        page.getByTestId('architecture-spoiler-toggle'),
        page.getByTestId('architecture-spoiler-content'));
    });
});
