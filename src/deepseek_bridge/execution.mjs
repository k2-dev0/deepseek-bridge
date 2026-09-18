// SDK 0.1.5rc1 drops every inherited name containing KEY, including Git's
// configuration names. Restore only indexed Git names, never general secrets.
export const name = 'bridge-execution-environment';
export const inject = ['subprocess'];

export function apply(ctx, config) {
  const service = ctx.subprocess;
  const originals = new Map();
  const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const environment = (spec) => {
    const env = { ...spec.env };
    const count = own(env, 'GIT_CONFIG_COUNT') ? env.GIT_CONFIG_COUNT : process.env.GIT_CONFIG_COUNT;
    if (typeof count === 'string' && /^(0|[1-9][0-9]*)$/.test(count)) {
      const limit = Number(count);
      if (Number.isSafeInteger(limit)) {
        for (const [key, value] of Object.entries(process.env)) {
          const match = /^GIT_CONFIG_KEY_(0|[1-9][0-9]*)$/.exec(key);
          if (match && Number(match[1]) < limit && !own(env, key)) env[key] = value;
        }
      }
    }
    // A caller's explicit undefined is an environment tombstone; preserve it.
    const path = own(env, 'PATH') ? env.PATH : process.env.PATH;
    if (typeof path === 'string') env.PATH = config.bin + ':' + path;
    return env;
  };
  for (const method of ['spawn', 'spawnTerminal']) {
    const original = service[method];
    originals.set(method, original);
    service[method] = function(spec) {
      return original.call(this, { ...spec, env: environment(spec) });
    };
  }
  ctx.on('dispose', () => {
    for (const [method, original] of originals) service[method] = original;
  });
}
