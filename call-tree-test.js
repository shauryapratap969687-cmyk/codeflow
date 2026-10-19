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
const structures = sandbox.CodeFlowStructures;
if (!structures) throw new Error('Structure test API was not exposed');

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

const beforeList = structures.structureModel([1, 2], 'list');
const afterList = structures.structureModel([1, 3, 4], 'list');
if (beforeList.kind !== 'list' || afterList.cells.length !== 3) throw new Error('List structure model was not preserved');
const listDiff = structures.compareStructure(beforeList, afterList);
if (!listDiff.some((cell) => cell.key === '[1]' && cell.status === 'changed') || !listDiff.some((cell) => cell.key === '[2]' && cell.status === 'added')) throw new Error('List cell changes were not identified');
if (structures.structureModel([1, 2], 'tuple').kind !== 'tuple' || structures.structureModel(['a'], 'set').kind !== 'set') throw new Error('Tuple/set type labels were not preserved');
const dictDiff = structures.compareStructure(structures.structureModel({ x: 1 }, 'dict'), structures.structureModel({ x: 2, y: 3 }, 'dict'));
if (!dictDiff.some((cell) => cell.key === 'x' && cell.status === 'changed') || !dictDiff.some((cell) => cell.key === 'y' && cell.status === 'added')) throw new Error('Dict cell changes were not identified');
if (structures.structureModel('abc', 'str').cells.length !== 3) throw new Error('Short strings were not split into characters');

console.log('Call-tree test passed: recursion, returns, repeats, cap, layout, empty state');
