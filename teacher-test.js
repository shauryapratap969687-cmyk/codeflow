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
  fetch: () => Promise.reject(new Error('not used in pure narration test')),
};
sandbox.window = sandbox;
vm.runInNewContext(source, sandbox, { filename: 'app.js' });

if (!sandbox.CodeFlowTeacher || typeof sandbox.CodeFlowTeacher.narrate !== 'function') {
  throw new Error('Teacher narration test API was not exposed');
}

const cases = [
  ['answer = 6 * 7', 'answer', 42],
  ['value -= 3', 'value', 7],
  ['items[1] = 2', 'items[1]', 2],
  ['total += item', 'total', 6],
  ['n = 5', 'n', 5],
  ['return value * 2', 'return', 8],
  ["text = 'hi!'", 'text', 'hi!'],
  ["lookup['x'] = 9", "lookup['x']", 9],
  ["print('handled')", 'output', 'handled'],
  ['count += 1', 'count', 2],
];

for (const [line, target, value] of cases) {
  const isReturn = target === 'return';
  const isOutput = target === 'output';
  const event = {
    type: isReturn ? 'return' : isOutput ? 'stdout' : 'line',
    line: 1,
    function: 'main',
    source_line: line,
    line_info: { kind: isReturn ? 'return' : isOutput ? 'expression' : line.includes('+=') || line.includes('-=') ? 'augassign' : 'assignment' },
    before_variables: isReturn || isOutput ? {} : { [target]: typeof value === 'number' ? value - 1 : 'before' },
    after_variables: isReturn || isOutput ? {} : { [target]: value },
    return_value: isReturn ? value : undefined,
    output_delta: isOutput ? `${value}\n` : '',
  };
  const text = sandbox.CodeFlowTeacher.narrate(event, null, 'en');
  if (!text || !text.trim()) throw new Error(`Empty narration for ${line}`);
  if (!isReturn && !isOutput && !text.includes(String(value))) throw new Error(`Narration lost recorded value for ${line}: ${text}`);
  if (isReturn && !text.includes(String(value))) throw new Error(`Narration lost return value for ${line}: ${text}`);
  if (isOutput && !text.includes(String(value))) throw new Error(`Narration lost output value for ${line}: ${text}`);
}

const hindi = sandbox.CodeFlowTeacher.narrate({
  type: 'line',
  line: 1,
  source_line: 'answer = 42',
  line_info: { kind: 'assignment' },
  before_variables: {},
  after_variables: { answer: 42 },
}, null, 'hi');
if (!/[\u0900-\u097F]/.test(hindi)) throw new Error(`Hindi narration was not Devanagari: ${hindi}`);

console.log(`Teacher narration test passed: ${cases.length + 1} cases`);
