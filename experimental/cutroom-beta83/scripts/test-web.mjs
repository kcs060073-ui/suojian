import {readdirSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {resolve} from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
export const webTests = readdirSync(resolve(root, 'tests'))
  .filter(name => name.endsWith('.test.mjs')).sort()
  .map(name => `tests/${name}`);
export const testArguments = ['--test', '--test-concurrency=1', ...webTests];
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.includes('--list')) console.log(webTests.join('\n'));
  else {
    if (!webTests.length) throw new Error('No web tests discovered');
    const result = spawnSync(process.execPath, testArguments, {cwd: root, stdio: 'inherit'});
    if (result.error) throw result.error;
    process.exitCode = result.status ?? 1;
  }
}
