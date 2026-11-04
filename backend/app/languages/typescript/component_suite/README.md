# Component tests

This standalone vitest project tests behaviour of a part of the repository, such as its
web client, that the HTTP contract tests cannot observe, at seams of the part's code: the
modules described by the `.d.ts` stubs in `contracts/parts/<part>/components/` of the
repository. The tests import the stubbed modules as `@part/<path within the part>`
(`@part/src/github` for `src/github.ts`) and drive them in-process in jsdom, with fakes of
every collaborator the stubs name. They never fake the seam itself.

The tests run against the part's code, so they are not run from this directory alone. To
type-check them against the stubs instead:

```sh
ln -sfn ../../contracts/parts/<part>/components .part
npm ci --ignore-scripts
npx tsc -p tsconfig.json
```

The trusted Docker runner installs the part from its lock file and this project from its
own, collects the suite during the build and runs it without a network when started, with
its own vitest configuration: `@part/` resolves to the part's directory and every test
runs in jsdom. Tests are `tests/**/*.test.ts` or `.test.tsx` files whose `test` or `it`
calls, and the `describe` calls around them, are named by string literals. The run is
limited to ten minutes, and the JUnit XML report is written to
`/tests/reports/junit.xml`.
