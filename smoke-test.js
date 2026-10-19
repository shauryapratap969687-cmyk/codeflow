const fs = require('fs');
const path = require('path');

const root = __dirname;
const required = ['index.html', 'styles.css', 'responsive.css', 'app.js', 'backend.py', 'README.md', 'package.json'];
for (const file of required) {
  if (!fs.existsSync(path.join(root, file))) throw new Error(`Missing ${file}`);
}

const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const js = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const backend = fs.readFileSync(path.join(root, 'backend.py'), 'utf8');
const checks = [
  ['editor', html.includes('id="code-editor"')],
  ['responsive viewport', html.includes('name="viewport"')],
  ['execute contract', js.includes("'/api/execute'")],
  ['verify contract', js.includes("'/api/verify'")],
  ['AI contract', js.includes("'/api/ai'")],
  ['offline fallback', js.includes('offlinePython')],
  ['startup guard', js.includes('DOMContentLoaded')],
  ['sound control', html.includes('id="sound-toggle"') && html.includes('id="sound-status"')],
  ['audio unlock', js.includes('AudioContext') && js.includes('toggleSound')],
  ['step sound path', js.includes('selectTimelineStep') && js.includes('playStepSound')],
  ['muted by default', js.includes('soundEnabled: false')],
  ['teacher mode controls', html.includes('id="teacher-simple"') && html.includes('id="teacher-detailed"')],
  ['value journey evidence', html.includes('id="value-journey"') && js.includes('lineInfoFor') && js.includes('textualSubstitution')],
  ['bilingual narration', js.includes('function narrate') && js.includes('hi-IN') && js.includes('प्रोग्राम')],
  ['speech preferences', js.includes('voiceschanged') && js.includes('codeflow.teacher.speed')],
  ['call tree view', html.includes('id="call-tree-view-tab"') && html.includes('id="call-tree-panel"') && js.includes('buildCallTree')],
  ['call tree accessibility', js.includes('role="button"') && js.includes('call-tree-node') && js.includes('prepareCallTree')],
  ['debug panel', html.includes('id="expected-output"') && html.includes('id="debug-compare"') && js.includes('compareDebugOutput')],
  ['quiz controls', html.includes('id="quiz-toggle"') && html.includes('id="quiz-panel"') && js.includes('quizQuestionForStep')],
  ['share and heat controls', html.includes('id="share-button"') && html.includes('id="line-heat-summary"') && js.includes('shareHashFor') && js.includes('lineHeatFromEvents')],
  ['demo gallery', ['buggy-sum', 'fibonacci', 'bubble-sort', 'string-reversal', 'list-dict', 'runtime-error'].every((name) => html.includes(`data-example="${name}"`) && (js.includes(`'${name}'`) || js.includes(`${name}:`)))]
];
if (/coldchain|cold-chain/i.test(html + js + backend)) throw new Error('Standalone folder contains an unrelated product reference');
const failed = checks.filter(([, passed]) => !passed);
if (failed.length) throw new Error(`Smoke test failed: ${failed.map(([name]) => name).join(', ')}`);
console.log(`Smoke test passed: ${checks.length} checks`);
