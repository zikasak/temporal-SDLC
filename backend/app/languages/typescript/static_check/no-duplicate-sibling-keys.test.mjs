// node --test: the rule against the shapes React pages render siblings in.
import { RuleTester } from 'eslint';
import tsParser from '@typescript-eslint/parser';
import rule from './no-duplicate-sibling-keys.mjs';

const tester = new RuleTester({
  languageOptions: { parser: tsParser, parserOptions: { ecmaFeatures: { jsx: true } } },
});

tester.run('no-duplicate-sibling-keys', rule, {
  valid: [
    // A key of its own for each sibling.
    `<main>{s && <A key={\`api-\${s.id}\`} />}{s && <B key={\`architecture-\${s.id}\`} />}</main>`,
    // Conditions that exclude each other: never rendered together.
    `<main>{s && s.only && <A key={s.id} />}{s && !s.only && <B key={s.id} />}</main>`,
    `<main>{s.only ? <A key={s.id} /> : <B key={s.id} />}</main>`,
    // An array child has keys of its own scope.
    `<main><A key="a" />{items.map((item) => <B key="a" />)}</main>`,
  ],
  invalid: [
    {
      // The page that showed a section once more on every poll.
      code: `<main>
        {session && <ApiModelSection key={session.session_id} />}
        {session?.architecture_structure && <ArchitectureSection key={session.session_id} />}
      </main>`,
      errors: [{ messageId: 'duplicate', line: 3 }],
    },
    {
      code: `<><A key="x" />{open && <B key={'x'} />}</>`,
      errors: [{ messageId: 'duplicate' }],
    },
  ],
});
