/**
 * Leave for GitHub's confirmation page of an app described by this instance's manifest:
 * keeps a fresh random state in sessionStorage, then appends to `document.body` a form
 * that posts to https://github.com/settings/apps/new?state=<state> (or
 * https://github.com/organizations/<organization>/settings/apps/new?state=<state> when
 * `organization` is given) with a hidden input named "manifest" holding the manifest as
 * JSON, and submits it. The manifest names the app `name`, is not public, and returns to
 * <window.location.origin>/github-app (`redirect_url` and `setup_url`). It requests the
 * permissions contents: write, metadata: read, pull_requests: write, issues: read,
 * checks: read and actions: read; turns the webhook on (`hook_attributes` with `active`
 * true and `url` <window.location.origin>/api/connectors/github/events); and subscribes
 * (`default_events`) to pull request conversation comments ("issue_comment"), inline
 * review comments ("pull_request_review_comment") and pull request events
 * ("pull_request"), so that merges and closes are seen.
 */
export declare function createGitHubApp(name: string, organization: string): void;
