// SDK 0.1.5rc1 uses these exact ps queries. Keep the persistent shell and OS fence.
import childProcess from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { isAbsolute } from 'node:path';

export const name = 'deepseek-bridge-macos-process-info';

export function apply(ctx, config) {
  if (process.platform !== 'darwin' || !isAbsolute(config.python) || !isAbsolute(config.helper)) {
    throw new Error('invalid macOS process compatibility configuration');
  }
  const original = childProcess.execFileSync;
  function execute(file, args, options) {
    if (file !== '/bin/ps') return original(file, args, options);
    let query;
    if (args.length === 2 && args[0] === '-axo' && args[1] === 'pid=,ppid=,lstart=') {
      query = ['table'];
    } else if (args.length === 4 && args[0] === '-o' && args[1] === 'tpgid=' &&
               args[2] === '-p' && /^\d+$/.test(args[3])) {
      query = ['foreground', args[3]];
    } else {
      throw new Error('unsupported DeepSeek process inspection query');
    }
    return original(config.python, ['-I', config.helper, ...query], { ...options, timeout: 5000 });
  }
  childProcess.execFileSync = execute;
  syncBuiltinESMExports();
  ctx.on('dispose', () => {
    childProcess.execFileSync = original;
    syncBuiltinESMExports();
  });
}
