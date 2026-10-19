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
  fetch: () => Promise.reject(new Error('not used in pure call-tree test')),
};
sandbox.window = sandbox;
vm.runInNewContext(source, sandbox, { filename: 'app.js' });

const treeApi = sandbox.CodeFlowCallTree;
if (!treeApi) throw new Error('Call-tree test API was not exposed');

const events = [
  { type: 'call', function: '<module>', call_stack: ['<module>'], variables: {} },
  { type: 'call', function: 'fib', call_stack: ['<module>', 'fib'], variables: { n: 2 } },
  { type: 'call', function: 'fib', call_stack: ['<module>', 'fib', 'fib'], variables: { n: 1 } },
  { type: 'return', function: 'fib', call_stack: ['<module>', 'fib', 'fib'], variables: { n: 1 }, return_value: 1 },
  { type: 'call', function: 'fib', call_stack: ['<module>', 'fib', 'fib'], variables: { n: 1 } },
  { type: 'return', function: 'fib', call_stack: ['<module>', 'fib', 'fib'], variables: { n: 1 }, return_value: 1 },
  { type: 'return', function: 'fib', call_stack: ['<module>', 'fib'], variables: { n: 2 }, return_value: 2 },
  { type: 'return', function: '<module>', call_stack: ['<module>'], variables: {}, return_value: null },
];

const tree = treeApi.buildCallTree(events);
if (!tree.hasFunctionCall) throw new Error('Function calls were not detected');
if (tree.nodes.length !== 4) throw new Error(`Expected four call nodes, got ${tree.nodes.length}`);
const repeated = tree.nodes.filter((node) => node.functionName === 'fib' && node.argumentsText === 'n=1');
if (repeated.length !== 2 || repeated.some((node) => node.repeatCount !== 2)) throw new Error('Repeated calls were not grouped');
if (tree.nodes.find((node) => node.functionName === 'fib' && node.argumentsText === 'n=1' && node.returnValue !== 1)) throw new Error('Return value was not attached to a call node');
if (tree.stepNodeIds[2] !== tree.stepNodeIds[3]) throw new Error('Step-to-node mapping did not follow the active call');

const limited = treeApi.prepareCallTree({ ...tree, nodes: Array.from({ length: 205 }, (_, index) => ({ id: `node-${index}`, parentId: index ? 'node-0' : null, children: [], depth: index ? 1 : 0, callSequence: index, functionName: 'f', argumentsText: '', label: 'f', repeatCount: 1 })) }, 20);
if (!limited.omittedCount || !limited.nodes.some((node) => node.collapsed && node.omittedCount === 186)) throw new Error('Large trees were not collapsed');
const layout = treeApi.layoutCallTree(limited);
if (!layout.positions.size || layout.width < 760 || layout.height < 110) throw new Error('Call-tree layout was not produced');

const empty = treeApi.buildCallTree([{ type: 'call', function: '<module>', call_stack: ['<module>'], variables: {} }]);
if (empty.hasFunctionCall) throw new Error('Module-only execution should show the friendly empty state');

console.log('Call-tree test passed: recursion, returns, repeats, cap, layout, empty state');
