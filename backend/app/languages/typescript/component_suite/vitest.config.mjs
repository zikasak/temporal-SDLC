// Trusted configuration of the runner: the suite's tests, run in jsdom, import the part's
// code as `@part/<path within the part>`. PART is the part's directory, REPORT where the
// JUnit report is written. It imports nothing but Node's own modules, so it loads from
// outside the suite.
import { readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';

if (!process.env.PART) {
  throw new Error('PART names the directory of the part under test');
}
const part = resolve(process.env.PART);
const manifest = JSON.parse(readFileSync(join(part, 'package.json'), 'utf8'));
// The packages the part depends on: a test importing one of them, such as react, gets
// the part's own copy, so that the tests and the code share one.
const dependencies = new Set(['dependencies', 'devDependencies', 'peerDependencies']
  .flatMap((key) => Object.keys(manifest[key] ?? {})));

function packageName(source) {
  const parts = source.split('/');
  return source.startsWith('@') ? parts.slice(0, 2).join('/') : parts[0];
}

const partDependencies = {
  name: 'part-dependencies',
  enforce: 'pre',
  async resolveId(source, importer, options) {
    if (!importer || importer.startsWith(`${part}/`) || /^[./]|^@part\/|:/.test(source)
        || !dependencies.has(packageName(source))) {
      return null;
    }
    return this.resolve(source, join(part, 'package.json'), { ...options, skipSelf: true });
  },
};

export default {
  plugins: [partDependencies],
  resolve: {
    alias: [{ find: /^@part\//, replacement: `${part}/` }],
  },
  test: {
    root: process.cwd(),
    include: ['tests/**/*.test.{ts,tsx}'],
    environment: 'jsdom',
    watch: false,
    reporters: ['default', 'junit'],
    outputFile: { junit: process.env.REPORT || 'reports/junit.xml' },
  },
};
