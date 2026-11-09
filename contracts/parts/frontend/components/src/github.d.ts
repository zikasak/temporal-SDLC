/**
 * Leave for GitHub's confirmation page of an app described by this instance's manifest:
 * appends to `document.body` a form that posts to
 * https://github.com/settings/apps/new (or
 * https://github.com/organizations/<organization>/settings/apps/new when `organization`
 * is given) with a hidden input named "manifest" holding the manifest as JSON, and
 * submits it. The manifest requests the permissions contents: write, metadata: read,
 * pull_requests: write and issues: read; turns the webhook on (`hook_attributes` with
 * `active` true and `url` <window.location.origin>/api/connectors/github/events); and
 * subscribes (`default_events`) to pull request conversation comments
 * ("issue_comment"), inline review comments ("pull_request_review_comment") and pull
 * request events ("pull_request"), so that merges and closes are seen.
 */
export declare function createGitHubApp(name: string, organization: string): void;
