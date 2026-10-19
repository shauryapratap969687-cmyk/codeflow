const fs = require('fs');
const vm = require('vm');
const { TextEncoder, TextDecoder } = require('util');

const source = fs.readFileSync('app.js', 'utf8');
const sandbox = {
  console,
  document: { readyState: 'loading', addEventListener() {} },
  setTimeout,
  clearTimeout,
  Promise,
  URLSearchParams,
  TextEncoder,
  TextDecoder,
  btoa: (value) => Buffer.from(value, 'binary').toString('base64'),
  atob: (value) => Buffer.from(value, 'base64').toString('binary'),
  fetch: () => Promise.reject(new Error('not used in share/heat test')),
};
sandbox.window = sandbox;
vm.runInNewContext(source, sandbox, { filename: 'app.js' });

const heat = sandbox.CodeFlowHeat;
const share = sandbox.CodeFlowShare;
if (!heat || !share) throw new Error('Share/heat test APIs were not exposed');

const counts = heat.lineHeatFromEvents([{ line: 1 }, { line: 2 }, { line: 2 }, { line: 2 }, { line: 4 }]);
if (counts[1] !== 1 || counts[2] !== 3 || !heat.lineHeatSummary(counts).includes('Line 2 ran 3 times')) throw new Error('Line heat counts or summary failed');
if (!heat.lineHeatSummary(counts, true).includes('recorded steps only')) throw new Error('Truncated heat summary failed');

const encoded = share.plainSharePayload('python', 'print("café ☕")');
if (!encoded || !encoded.startsWith('p.')) throw new Error('Plain share encoding failed');
const decoded = share.decodePlainSharePayload(encoded.slice(2));
if (!decoded || decoded.language !== 'python' || decoded.code !== 'print("café ☕")') throw new Error('Plain share decoding lost Unicode code');
if (share.plainSharePayload('python', 'x'.repeat(64 * 1024 + 1)) !== null) throw new Error('Oversized share source was not rejected');

(async () => {
  const hash = await share.shareHashFor('javascript', 'console.log(1)');
  const loaded = await share.decodeShareHash(hash);
  if (!loaded || loaded.language !== 'javascript' || loaded.code !== 'console.log(1)') throw new Error('Share hash round trip failed');
  if (await share.decodeShareHash('#codeflow=1.p.invalid') !== null) throw new Error('Invalid share hash was accepted');
  console.log('Share/heat test passed: counts, truncation, Unicode round trip, size guard, invalid hash');
})().catch((error) => { console.error(error); process.exitCode = 1; });
