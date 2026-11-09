import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { createGitHubApp } from '@part/src/github';

type Submission = {
  action: string;
  method: string;
  manifest: {
    default_permissions?: Record<string, string>;
    default_events?: string[];
    hook_attributes?: { url?: string; active?: boolean };
  };
};

let submissions: Submission[] = [];

function capture(form: HTMLFormElement): void {
  const input = form.querySelector('input[name="manifest"]') as HTMLInputElement | null;
  expect(input, 'the form must hold a hidden input named "manifest"').not.toBeNull();
  submissions.push({
    action: form.action,
    method: form.method.toLowerCase(),
    manifest: JSON.parse((input as HTMLInputElement).value),
  });
}

/** The form's address without a query string or fragment (a page may add a state value). */
function addressWithoutQuery(action: string): string {
  const url = new URL(action);
  return url.origin + url.pathname;
}

beforeEach(() => {
  submissions = [];
  // jsdom does not navigate: record what the page would have posted to GitHub.
  vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(function (this: HTMLFormElement) {
    capture(this);
  });
  vi.spyOn(HTMLFormElement.prototype, 'requestSubmit').mockImplementation(function (this: HTMLFormElement) {
    capture(this);
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

describe('createGitHubApp manifest', () => {
  test('posts to the personal account page a manifest with the pull request permission, the webhook and the pull request events', async () => {
    createGitHubApp('Flows App', '');

    await vi.waitFor(() => expect(submissions).toHaveLength(1));
    const { action, method, manifest } = submissions[0];
    expect(addressWithoutQuery(action)).toBe('https://github.com/settings/apps/new');
    expect(method).toBe('post');
    expect(manifest.default_permissions).toMatchObject({
      contents: 'write',
      metadata: 'read',
      pull_requests: 'write',
      issues: 'read',
    });
    expect(manifest.hook_attributes).toMatchObject({
      active: true,
      url: `${window.location.origin}/api/connectors/github/events`,
    });
    expect(manifest.default_events).toEqual(
      expect.arrayContaining(['issue_comment', 'pull_request_review_comment', 'pull_request']),
    );
  });

  test('posts to the organization page a manifest that subscribes to pull request events', async () => {
    createGitHubApp('Flows App', 'acme-org');

    await vi.waitFor(() => expect(submissions).toHaveLength(1));
    const { action, method, manifest } = submissions[0];
    expect(addressWithoutQuery(action)).toBe('https://github.com/organizations/acme-org/settings/apps/new');
    expect(method).toBe('post');
    expect(manifest.default_events).toEqual(
      expect.arrayContaining(['issue_comment', 'pull_request_review_comment', 'pull_request']),
    );
    expect(manifest.hook_attributes).toMatchObject({
      active: true,
      url: `${window.location.origin}/api/connectors/github/events`,
    });
  });
});
