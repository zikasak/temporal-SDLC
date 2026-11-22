"""The container images the sandbox worker pulls ahead of use, by digest, so that no
delivery waits for a large pull and a registry outage does not fail a run
(plans/browser-route.md, section 3.8). No other module may import anything here but
these constants: the languages generate their Dockerfiles' base images from them.

Upgrading one is a deliberate change of its reference here and of whatever must agree
with it (the browser suite's `@playwright/test` version, checked by a backend test). The
next worker start pulls the new image; the old one stays until a person removes it, so a
delivery still running on it is not broken mid-run. Any cleanup of the sandbox's Docker
daemon must spare every image of PREPULLED_IMAGES."""

# Playwright's runner image, whose bundled Chromium and fonts are the ones
# `@playwright/test` 1.65.1 expects: the base of the browser tests' runner and of the
# page contract's conformance check. The digest is the multi-arch index's.
PLAYWRIGHT_VERSION = '1.65.1'
PLAYWRIGHT_IMAGE = (
    f'mcr.microsoft.com/playwright:v{PLAYWRIGHT_VERSION}-noble'
    '@sha256:1507cbfc550a63a6b7396791ca4f00df17beb56342d2447b3f106c306f525579')

PREPULLED_IMAGES: tuple[str, ...] = (PLAYWRIGHT_IMAGE,)
