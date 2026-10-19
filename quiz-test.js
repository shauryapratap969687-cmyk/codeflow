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
  fetch: () => Promise.reject(new Error('not used in pure quiz test')),
};
sandbox.window = sandbox;
vm.runInNewContext(source, sandbox, { filename: 'app.js' });
const quiz = sandbox.CodeFlowQuiz;
if (!quiz) throw new Error('Quiz test API was not exposed');

if (!quiz.quizAnswersMatch(' "7" ', 7) || !quiz.quizAnswersMatch('true', true) || quiz.quizAnswersMatch('seven', 7)) throw new Error('Quiz answer normalization failed');
if (!quiz.quizAnswerValue(1) || !quiz.quizAnswerValue([1, 'two']) || quiz.quizAnswerValue({ value: 1 })) throw new Error('Quiz scalar boundary failed');

const valueEvents = [
  { type: 'line', line: 1, function: '<module>', line_info: { kind: 'assignment', writes: ['total'], reads: ['base'] }, before_variables: { base: 2, total: 1 }, after_variables: { base: 2, total: 3 } },
  { type: 'return', line: 2, function: '<module>', line_info: { kind: 'return' }, before_variables: { base: 2, total: 3 }, after_variables: { base: 2, total: 3 }, return_value: null }
];
const valueQuestion = quiz.quizQuestionForStep(valueEvents, 0, 'total = base + 1\nreturn None');
if (!valueQuestion || valueQuestion.kind !== 'value' || valueQuestion.target !== 'total' || valueQuestion.expected !== 3 || !valueQuestion.question.includes('total')) throw new Error('Value quiz question was not derived from trace');

const conditionEvents = [
  { type: 'line', line: 1, function: '<module>', line_info: { kind: 'if', writes: [], reads: ['n'] }, before_variables: { n: 2 }, after_variables: { n: 2 } },
  { type: 'line', line: 2, function: '<module>', line_info: { kind: 'expression', writes: [], reads: [] }, before_variables: { n: 2 }, after_variables: { n: 2 } }
];
const conditionQuestion = quiz.quizQuestionForStep(conditionEvents, 0, 'if n > 0:\n    print(n)\nprint("done")');
if (!conditionQuestion || conditionQuestion.kind !== 'condition' || conditionQuestion.expected !== true) throw new Error('Condition quiz question was not derived from branch trace');

const unknown = quiz.quizQuestionForStep([{ type: 'line', line: 1, line_info: { kind: 'assignment', writes: ['huge'] }, before_variables: {}, after_variables: { huge: 'x'.repeat(81) } }], 0, 'huge = value');
if (unknown) throw new Error('Quiz asked for an unsupported long value');

console.log('Quiz test passed: normalization, scalar boundary, value, condition, unknown skip');
