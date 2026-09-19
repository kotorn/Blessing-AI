import js from '@eslint/js';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  {
    // Generated SDK, build output, and pytest scratch dirs are not reviewable source.
    ignores: ['dist/', 'node_modules/', 'src/dataconnect-generated/', '.pytest_temp*/', '.pytest-tmp*/'],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    languageOptions: {
      globals: { ...globals.node, ...globals.browser },
    },
    rules: {
      // Empty catch blocks are an accepted fail-open idiom in this codebase
      // (mirrors the Python side); flag only empty statement blocks.
      'no-empty': ['error', { allowEmptyCatch: true }],
      // The codebase predates this gate; tighten incrementally rather than
      // bulk-annotating unrelated modules in one PR. no-unused-vars stays at
      // warn until the existing ~260 UI findings are ratcheted down.
      '@typescript-eslint/no-explicit-any': 'off',
      '@typescript-eslint/no-unused-vars': [
        'warn',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
    },
  }
);
