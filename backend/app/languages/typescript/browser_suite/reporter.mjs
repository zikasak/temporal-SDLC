// Trusted JUnit reporter of the runner. A test's class name is its file, from the suite's
// directory, and the viewport (the Playwright project) it ran in, as `tests/a.spec.ts#narrow`,
// after CLASSNAME_PREFIX when set; its name is the names of the describe blocks around it
// and its own, joined by " > ". So a failure says which window size failed, and the host
// identifies the test as `tests/a.spec.ts#narrow > Outer > test`.
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, relative } from 'node:path';

const ANSI = /\u001b\[[0-9;]*[A-Za-z]/g;

function escape(text) {
  return String(text)
    // Characters XML 1.0 cannot hold.
    .replace(/[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd\u{10000}-\u{10ffff}]/gu, '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function describePath(test) {
  const names = [];
  for (let suite = test.parent; suite && suite.type === 'describe'; suite = suite.parent) {
    names.unshift(suite.title);
  }
  return [...names, test.title].join(' > ');
}

export default class JUnitReporter {
  constructor(options = {}) {
    this.outputFile = options.outputFile || process.env.REPORT || 'reports/junit.xml';
    this.prefix = options.prefix ?? process.env.CLASSNAME_PREFIX ?? '';
    this.root = process.cwd();
    this.cases = [];
    this.errors = [];
  }

  printsToStdio() {
    return false;
  }

  onTestEnd(test, result) {
    const project = test.parent.project()?.name || 'default';
    const file = relative(this.root, test.location.file).split('\\').join('/');
    const outcome = test.outcome();
    const error = result.errors[0] || result.error;
    const message = (error?.message || (result.status === 'timedOut' ? 'Test timed out' : '')).replace(ANSI, '');
    this.cases.push({
      classname: `${this.prefix}${file}#${project}`,
      name: describePath(test),
      time: result.duration / 1000,
      outcome: outcome === 'skipped' ? 'skipped' : outcome === 'unexpected' ? 'failure' : 'passed',
      message,
      detail: (error?.stack || message).replace(ANSI, ''),
    });
  }

  onError(error) {
    this.errors.push((error.stack || error.message || String(error)).replace(ANSI, ''));
  }

  onEnd() {
    const cases = [...this.cases];
    this.errors.forEach((detail, index) => cases.push({
      classname: `${this.prefix}errors`, name: `error ${index + 1}`, time: 0,
      outcome: 'error', message: detail.split('\n')[0], detail,
    }));
    const count = (outcome) => cases.filter((item) => item.outcome === outcome).length;
    const lines = cases.map((item) => {
      const head = `    <testcase classname="${escape(item.classname)}" name="${escape(item.name)}" time="${item.time.toFixed(3)}"`;
      if (item.outcome === 'passed') return `${head}/>`;
      if (item.outcome === 'skipped') return `${head}>\n      <skipped/>\n    </testcase>`;
      return `${head}>\n      <${item.outcome} message="${escape(item.message.slice(0, 2000))}">${escape(item.detail.slice(0, 8000))}</${item.outcome}>\n    </testcase>`;
    });
    const xml = [
      '<?xml version="1.0" encoding="UTF-8"?>',
      `<testsuites tests="${cases.length}" failures="${count('failure')}" errors="${count('error')}" skipped="${count('skipped')}">`,
      `  <testsuite name="browser tests" tests="${cases.length}" failures="${count('failure')}" errors="${count('error')}" skipped="${count('skipped')}">`,
      ...lines,
      '  </testsuite>',
      '</testsuites>',
      '',
    ].join('\n');
    mkdirSync(dirname(this.outputFile), { recursive: true });
    writeFileSync(this.outputFile, xml);
  }
}
