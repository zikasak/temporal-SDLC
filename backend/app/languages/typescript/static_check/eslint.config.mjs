// The TypeScript static check: ESLint limited to errors and likely bugs, never style, run
// over the trees under review before the delivery review (app/languages/static_checks.py)
// and by this repository's CI over its frontend. Given with --config, its file patterns
// are relative to the directory ESLint runs in.
import tsParser from '@typescript-eslint/parser';
import react from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';
import noDuplicateSiblingKeys from './no-duplicate-sibling-keys.mjs';

export default [
  {
    ignores: ['**/node_modules/**', '**/dist/**', '**/build/**', '**/coverage/**',
      '**/.vite/**', '**/.next/**', '**/.turbo/**'],
  },
  {
    files: ['**/*.{js,jsx,mjs,cjs,ts,tsx,mts,cts}'],
    languageOptions: {
      parser: tsParser,
      // JSX in .js and .jsx; TypeScript files take it from their extension.
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    // The code under review cannot switch the rules off, and its own disable comments,
    // naming rules of other configurations, are not errors here.
    linterOptions: { noInlineConfig: true, reportUnusedDisableDirectives: 'off' },
    plugins: {
      react,
      'react-hooks': reactHooks,
      sdlc: { rules: { 'no-duplicate-sibling-keys': noDuplicateSiblingKeys } },
    },
    // A fixed version: detecting it would read the project's node_modules.
    settings: { react: { version: '19.0' } },
    rules: {
      'sdlc/no-duplicate-sibling-keys': 'error',
      'react/jsx-key': ['error', { checkFragmentShorthand: true, warnOnDuplicates: true }],
      'react/jsx-no-duplicate-props': 'error',
      'react/no-direct-mutation-state': 'error',
      'react/no-danger-with-children': 'error',
      'react/void-dom-elements-no-children': 'error',
      'react-hooks/rules-of-hooks': 'error',
      'no-compare-neg-zero': 'error',
      'no-cond-assign': 'error',
      'no-constant-binary-expression': 'error',
      'no-dupe-else-if': 'error',
      'no-dupe-keys': 'error',
      'no-duplicate-case': 'error',
      'no-loss-of-precision': 'error',
      'no-self-assign': 'error',
      'no-self-compare': 'error',
      'no-sparse-arrays': 'error',
      'no-unreachable': 'error',
      'no-unsafe-finally': 'error',
      'no-unsafe-negation': 'error',
      'no-unsafe-optional-chaining': 'error',
      'use-isnan': 'error',
      'valid-typeof': 'error',
    },
  },
];
