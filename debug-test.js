const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('app.js', 'utf8');
const sandbox = {
  console,
  document: { readyState: 'loading', addEventListener() {} },
  setTimeout,
  clearTimeout,
  Promise,
  URLSearchParams,
  fetch: () => Promise.reject(new Error('not used in pure debug test')),
};
sandbox.window = sandbox;
vm.runInNewContext(source, sandbox, { filename: 'app.js' });
const debug = sandbox.CodeFlowDebug;
if (!debug) throw new Error('Debug test API was not exposed');

let result = debug.compareDebugOutput('one\ntwo', 'one\ntwo');
if (!result.matches || result.firstMismatch !== -1) throw new Error('Matching output was reported as different');

result = debug.compareDebugOutput('one two', 'one\ttwo');
if (result.matches || !result.whitespaceOnly || result.expectedText !== 'one·two' || !result.actualText.includes('→')) throw new Error('Whitespace mismatch was not shown visibly');

result = debug.compareDebugOutput('', 'extra');
if (result.matches || result.firstMismatch !== 0 || result.expectedCount !== 0) throw new Error('Empty expected output was not handled');

const events = [
  { sequence: 0, type: 'line', line: 1, function: '<module>', output_delta: '' },
  { sequence: 1, type: 'line', line: 2, function: '<module>', output_delta: 'first\nsecond\n' },
];
const map = debug.outputEventMap(events);
if (map.length !== 2 || map[0].eventIndex !== 1 || map[1].line !== 2) throw new Error('Output lines were not mapped to trace events');

const summary = debug.debugSummary({
  comparison: debug.compareDebugOutput('7', '6'),
  selectedEvent: { line: 5 },
  selectedIndex: 1,
  reads: [],
  truncated: false,
  errorIndex: -1
});
if (!summary.includes('Line 1') || !summary.includes('6') || !summary.includes('7')) throw new Error('Deterministic debug summary was incomplete');

console.log('Debug test passed: matching, whitespace, empty output, mapping, summary');
