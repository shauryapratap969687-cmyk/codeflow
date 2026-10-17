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
  ['muted by default', js.includes('soundEnabled: false')]
];
if (/coldchain|cold-chain/i.test(html + js + backend)) throw new Error('Standalone folder contains an unrelated product reference');
const failed = checks.filter(([, passed]) => !passed);
if (failed.length) throw new Error(`Smoke test failed: ${failed.map(([name]) => name).join(', ')}`);
console.log(`Smoke test passed: ${checks.length} checks`);
