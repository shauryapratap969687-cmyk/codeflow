(() => {
  'use strict';

  const state = {
    source: '',
    snapshot: '',
    step: 0,
    playing: false,
    timer: null,
    language: 'python',
    apiOnline: null,
    history: [],
    trace: [],
    diagnostics: [],
    lastResult: null,
    executedSource: '',
    activeLine: null,
    audioContext: null,
    soundMasterGain: null,
    soundCompressor: null,
    activeVoices: new Set(),
    soundEnabled: false,
    soundUnlocking: false,
    animationFrame: null,
    animationToken: 0,
    soundPulseToken: 0,
    playbackToken: 0
  };

  const $ = (id) => document.getElementById(id);
  const all = (selector) => Array.from(document.querySelectorAll(selector));

  function showToast(message) {
    const toast = $('toast');
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add('show');
    window.clearTimeout(showToast.timeout);
    showToast.timeout = window.setTimeout(() => toast.classList.remove('show'), 3200);
  }

  function setText(id, value) {
    const element = $(id);
    if (element) element.textContent = value;
  }

  function audioContextConstructor() {
    return window.AudioContext || window.webkitAudioContext || null;
  }

  function setSoundStatus(message) {
    setText('sound-status', message);
  }

  function pulseSoundStatus(event) {
    const status = $('sound-status');
    const button = $('sound-toggle');
    const token = ++state.soundPulseToken;
    const type = eventType(event);
    setSoundStatus(`Sound on · ${type} tone fired`);
    [status, button].filter(Boolean).forEach((element) => {
      element.classList.remove('sound-fired');
      element.dataset.toneType = type;
    });
    window.requestAnimationFrame(() => {
      if (token !== state.soundPulseToken) return;
      [status, button].filter(Boolean).forEach((element) => element.classList.add('sound-fired'));
      window.setTimeout(() => {
        if (token !== state.soundPulseToken) return;
        [status, button].filter(Boolean).forEach((element) => element.classList.remove('sound-fired'));
      }, 460);
    });
  }

  function updateSoundControl() {
    const button = $('sound-toggle');
    if (!button) return;
    const supported = Boolean(audioContextConstructor());
    button.disabled = !supported || state.soundUnlocking;
    button.textContent = state.soundEnabled ? '🔊 Sound on' : '🔇 Sound off';
    button.setAttribute('aria-pressed', String(state.soundEnabled));
    button.title = state.soundEnabled ? 'Mute trace sounds' : 'Enable trace sounds';
  }

  function configureSoundSupport() {
    if (!audioContextConstructor()) {
      setSoundStatus('Audio unavailable in this browser');
      updateSoundControl();
      return;
    }
    setSoundStatus('Sound off · enable to hear trace steps');
    updateSoundControl();
  }

  function ensureAudioContext() {
    if (state.audioContext) return state.audioContext;
    const AudioContext = audioContextConstructor();
    if (!AudioContext) return null;
    try {
      state.audioContext = new AudioContext();
      state.soundMasterGain = state.audioContext.createGain();
      state.soundCompressor = state.audioContext.createDynamicsCompressor();
      state.soundMasterGain.gain.setValueAtTime(0.12, state.audioContext.currentTime);
      state.soundCompressor.threshold.setValueAtTime(-18, state.audioContext.currentTime);
      state.soundCompressor.knee.setValueAtTime(24, state.audioContext.currentTime);
      state.soundCompressor.ratio.setValueAtTime(4, state.audioContext.currentTime);
      state.soundCompressor.attack.setValueAtTime(0.003, state.audioContext.currentTime);
      state.soundCompressor.release.setValueAtTime(0.18, state.audioContext.currentTime);
      state.soundMasterGain.connect(state.soundCompressor);
      state.soundCompressor.connect(state.audioContext.destination);
      return state.audioContext;
    } catch (_) {
      state.audioContext = null;
      state.soundMasterGain = null;
      state.soundCompressor = null;
      return null;
    }
  }

  function stopActiveVoices() {
    const context = state.audioContext;
    const now = context && context.state !== 'closed' ? context.currentTime : 0;
    state.activeVoices.forEach((voice) => {
      try {
        if (context && context.state !== 'closed') {
          voice.gain.gain.cancelScheduledValues(now);
          voice.gain.gain.setTargetAtTime(0.0001, now, 0.008);
          voice.oscillator.stop(Math.max(now + 0.035, voice.start + 0.035));
        } else {
          voice.oscillator.disconnect();
          voice.gain.disconnect();
        }
      } catch (_) { /* an already-ended voice is safe to discard */ }
    });
    state.activeVoices.clear();
  }

  function disableSound(message) {
    stopActiveVoices();
    state.soundEnabled = false;
    state.soundUnlocking = false;
    updateSoundControl();
    setSoundStatus(message);
  }

  async function toggleSound() {
    if (state.soundUnlocking) return;
    if (state.soundEnabled) {
      disableSound('Sound off · enable to hear trace steps');
      return;
    }
    const context = ensureAudioContext();
    if (!context) {
      disableSound('Audio unavailable in this browser');
      return;
    }
    state.soundUnlocking = true;
    updateSoundControl();
    setSoundStatus('Enabling sound…');
    try {
      if (context.state === 'suspended') await context.resume();
      if (context.state === 'closed') throw new Error('AudioContext is closed');
      state.soundEnabled = true;
      setSoundStatus('Sound on · trace steps enabled');
    } catch (_) {
      disableSound('Sound could not start in this browser');
    } finally {
      state.soundUnlocking = false;
      updateSoundControl();
    }
  }

  function playStepSound(event) {
    if (!state.soundEnabled || !state.audioContext) return;
    const context = state.audioContext;
    if (context.state === 'closed') {
      disableSound('Sound unavailable in this browser');
      return;
    }
    const type = eventType(event).toLowerCase();
    const profile = type.includes('exception') || type.includes('error') || type.includes('stderr') || type.includes('throw')
      ? { notes: [{ frequency: 220, offset: 0 }, { frequency: 165, offset: 0.09 }, { frequency: 130, offset: 0.18 }], waveform: 'triangle', duration: 0.31 }
      : type.includes('stdout') || type.includes('output') || type.includes('log')
        ? { notes: [{ frequency: 660, offset: 0 }, { frequency: 880, offset: 0.085 }], waveform: 'sine', duration: 0.22 }
        : type.includes('call')
          ? { notes: [{ frequency: 440, offset: 0 }, { frequency: 660, offset: 0.09 }], waveform: 'triangle', duration: 0.23 }
          : type.includes('return')
            ? { notes: [{ frequency: 550, offset: 0 }, { frequency: 330, offset: 0.1 }], waveform: 'triangle', duration: 0.24 }
            : { notes: [{ frequency: 350, offset: 0 }, { frequency: 440, offset: 0.085 }], waveform: 'sine', duration: 0.22 };

    const emit = () => {
      if (!state.soundEnabled || context.state === 'closed') return;
      try {
        if (!state.soundMasterGain || !state.soundCompressor) ensureAudioContext();
        const now = context.currentTime;
        stopActiveVoices();
        profile.notes.forEach((note, index) => {
          const oscillator = context.createOscillator();
          const gain = context.createGain();
          const start = now + note.offset;
          const end = now + profile.duration;
          const level = index === 0 ? 0.24 : 0.18;
          oscillator.type = profile.waveform;
          oscillator.frequency.setValueAtTime(note.frequency, start);
          oscillator.frequency.exponentialRampToValueAtTime(note.frequency * 0.985, end);
          gain.gain.setValueAtTime(0.0001, start);
          gain.gain.exponentialRampToValueAtTime(level, start + 0.014);
          gain.gain.setValueAtTime(level, Math.max(start + 0.016, end - 0.045));
          gain.gain.exponentialRampToValueAtTime(0.0001, end);
          oscillator.connect(gain);
          gain.connect(state.soundMasterGain);
          const voice = { oscillator, gain, start };
          state.activeVoices.add(voice);
          oscillator.start(start);
          oscillator.stop(end + 0.025);
          oscillator.addEventListener('ended', () => {
            state.activeVoices.delete(voice);
            oscillator.disconnect();
            gain.disconnect();
          }, { once: true });
        });
        pulseSoundStatus(event);
      } catch (_) {
        disableSound('Sound could not play in this browser');
      }
    };

    if (context.state === 'suspended') {
      context.resume().then(emit).catch(() => disableSound('Sound paused by the browser'));
      return;
    }
    emit();
  }

  function updateLineNumbers() {
    const editor = $('code-editor');
    const numbers = $('line-numbers');
    if (!editor || !numbers) return;
    const count = Math.max(1, editor.value.split('\n').length);
    numbers.innerHTML = Array.from({ length: count }, (_, index) => `<span class="${index + 1 === state.activeLine ? 'active' : ''}">${String(index + 1).padStart(2, '0')}</span>`).join('\n');
  }

  function updateCursor() {
    const editor = $('code-editor');
    if (!editor) return;
    const before = editor.value.slice(0, editor.selectionStart);
    const lines = before.split('\n');
    setText('cursor-position', `Ln ${lines.length}, Col ${lines[lines.length - 1].length + 1}`);
  }

  function saveSnapshot() {
    const editor = $('code-editor');
    if (!editor) return;
    state.source = editor.value;
    state.history.push(state.source);
    if (state.history.length > 20) state.history.shift();
    try { localStorage.setItem('codeflow.source', state.source); } catch (_) { /* persistence is optional */ }
    setText('save-state', 'Unsaved changes');
    window.clearTimeout(saveSnapshot.timeout);
    saveSnapshot.timeout = window.setTimeout(() => setText('save-state', 'All changes saved'), 650);
  }

  function renderVariables(variables) {
    const list = $('variables-list');
    if (!list) return;
    const entries = Object.entries(variables || {});
    setText('variable-count', String(entries.length));
    list.innerHTML = entries.length ? entries.map(([name, value]) => {
      const type = Array.isArray(value) ? 'list' : value === null ? 'none' : typeof value;
      const display = typeof value === 'string' ? `"${value}"` : JSON.stringify(value);
      return `<div class="data-row"><span class="data-type">${type}</span><b>${escapeHtml(name)}</b><code>${escapeHtml(display)}</code></div>`;
    }).join('') : '<div class="data-row"><span class="data-type">—</span><b>No variables</b><code>run code first</code></div>';
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>'"]/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[character]));
  }

  function renderConsole(lines, fallback = false) {
    const output = $('console-output');
    if (!output) return;
    const safeLines = Array.isArray(lines) ? lines : [String(lines || '')];
    output.innerHTML = safeLines.length ? safeLines.map((line) => `<div class="console-line"><span>›</span><span>${escapeHtml(line)}</span></div>`).join('') : '<div class="console-line muted-line"><span>›</span><span>No output</span></div>';
    if (fallback) showToast('API unavailable — ran the offline Python subset.');
  }

  function renderDiagnostics(diagnostics, ok = true) {
    const list = $('diagnostics-output');
    const status = $('diagnostic-status');
    if (!list || !status) return;
    const items = Array.isArray(diagnostics) ? diagnostics.filter(Boolean) : diagnostics ? [diagnostics] : [];
    state.diagnostics = items;
    status.className = ok ? 'diagnostic-ok' : 'diagnostic-ok issue';
    status.textContent = ok ? '● No issues' : `● ${items.length} issue${items.length === 1 ? '' : 's'}`;
    list.innerHTML = items.length ? items.map((item) => {
      const line = Number.isInteger(item && item.line) ? ` data-line="${item.line}"` : '';
      return `<div class="diagnostic-line"${line} tabindex="${line ? '0' : '-1'}"><span class="check">${ok ? '✓' : '!'}</span><div><b>${escapeHtml(item.title || item.message || item)}</b><small>${escapeHtml(item.detail || 'Review this line and try again.')}${item.line ? ` · line ${item.line}${item.column ? `:${item.column}` : ''}` : ''}</small></div></div>`;
    }).join('') : '<div class="diagnostic-line"><span class="check">✓</span><div><b>Code looks good</b><small>Ready to execute your program.</small></div></div>';
    const firstLine = items[0] && Number(items[0].line);
    if (Number.isInteger(firstLine) && firstLine > 0) focusSourceLine(firstLine);
  }

  function setApiStatus(online) {
    state.apiOnline = online;
    const badge = $('api-badge');
    const note = $('offline-note');
    if (!badge || !note) return;
    badge.textContent = online ? 'API ready' : 'Offline mode';
    badge.classList.toggle('offline', !online);
    note.innerHTML = online ? '<span>●</span> API connected · offline fallback remains available' : '<span>◌</span> Offline fallback available for this Python subset';
  }

  function setAiProviderStatus(kind, detail) {
    const badge = $('ai-provider-status');
    const labels = { remote: 'Remote AI', local: 'Local fallback', notConfigured: 'Not configured', error: 'Remote AI · error' };
    if (badge) {
      badge.textContent = labels[kind] || labels.notConfigured;
      badge.className = `ai-provider-badge ${kind}`;
    }
    if (detail) setText('ai-status', detail);
  }

  function aiContext(prompt) {
    return {
      mode: 'chat',
      prompt,
      language: state.language,
      code: $('code-editor').value,
      diagnostics: state.diagnostics,
      selected_event: state.trace.length ? (state.trace[state.step] || null) : null
    };
  }

  function renderAiProposal(proposed) {
    const container = $('ai-proposal');
    if (!container) return;
    if (!proposed || typeof proposed.code !== 'string') {
      container.classList.add('hidden');
      container.innerHTML = '';
      return;
    }
    const language = proposed.language || state.language;
    container.classList.remove('hidden');
    container.innerHTML = `<div class="proposal-heading"><b>Proposed ${escapeHtml(language)} code</b><button class="button button-ghost small-button" type="button">Apply</button></div><pre>${escapeHtml(proposed.code)}</pre>${proposed.explanation ? `<small>${escapeHtml(proposed.explanation)}</small>` : ''}`;
    const apply = container.querySelector('button');
    if (apply) apply.addEventListener('click', () => {
      const editor = $('code-editor');
      editor.value = proposed.code;
      if (language === 'python' || language === 'javascript') {
        state.language = language;
        $('language-select').value = language;
        $('file-name').textContent = language === 'python' ? 'main.py' : 'main.js';
      }
      updateLineNumbers();
      updateCursor();
      saveSnapshot();
      showToast('Proposed code applied to the editor.');
    });
  }

  function localAiFallback(prompt) {
    const event = state.trace[state.step];
    if (event) {
      const label = eventLabel(event, state.step);
      return `Local fallback (no LLM): the selected ${label.type} event is ${label.line.toLowerCase()}. Inspect the highlighted source line and its recorded variables for evidence about “${prompt}”.`;
    }
    const diagnostics = state.diagnostics;
    if (diagnostics.length) return `Local fallback (no LLM): the execution service reported ${diagnostics.length} diagnostic${diagnostics.length === 1 ? '' : 's'}. Review the Diagnostics panel for the recorded issue before changing the code.`;
    return 'Local fallback (no LLM): run the code and select a trace event to get an evidence-based explanation. No runtime claim was generated.';
  }

  async function refreshHealth() {
    try {
      const response = await fetch('/api/health');
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const health = await response.json();
      setApiStatus(true);
      if (health.ai_configured) setAiProviderStatus('remote', 'Remote AI · provider credentials detected');
      else setAiProviderStatus('notConfigured', 'Not configured · set CODEFLOW_AI_API_KEY on the backend');
    } catch (_) {
      setApiStatus(false);
      setAiProviderStatus('local', 'Local fallback · backend unavailable');
    }
  }

  function focusSourceLine(line) {
    const editor = $('code-editor');
    const lineNumber = Number(line);
    if (!editor || !Number.isInteger(lineNumber) || lineNumber < 1) return;
    state.activeLine = lineNumber;
    updateLineNumbers();
    const lines = editor.value.split('\n');
    const start = lines.slice(0, lineNumber - 1).reduce((total, value) => total + value.length + 1, 0);
    editor.focus();
    editor.setSelectionRange(start, start + (lines[lineNumber - 1] || '').length);
    editor.scrollTop = Math.max(0, (lineNumber - 1) * parseFloat(window.getComputedStyle(editor).lineHeight || '20') - editor.clientHeight / 3);
    setText('cursor-position', `Ln ${lineNumber}, Col 1`);
  }

  function eventLabel(event, index) {
    const lineNumber = eventLine(event);
    const line = Number.isInteger(lineNumber) ? `Line ${lineNumber}` : `Step ${index + 1}`;
    const type = eventType(event);
    return { line, type };
  }

  function eventType(event) {
    const rawValue = String(event && typeof event === 'object' ? (event.type || event.event || event.kind || '') : '');
    const method = String(event && typeof event === 'object' ? (event.method || event.operation || '') : '').trim().toLowerCase();
    const raw = rawValue.replace(/[._-]/g, ' ').trim().toLowerCase();
    if (!raw) return 'unknown';
    if (raw === 'console' && (method === 'log' || method === 'info')) return 'stdout';
    if (raw === 'console' && (method === 'warn' || method === 'error')) return 'stderr';
    if (raw === 'console log' || raw === 'console info' || raw === 'log' || raw === 'info') return 'stdout';
    if (raw === 'console warn' || raw === 'console error' || raw === 'stderr' || raw === 'warning') return 'stderr';
    if (raw === 'throw' || raw === 'thrown' || raw === 'uncaught') return 'exception';
    if (raw === 'step' || raw === 'statement' || raw === 'execution') return 'line';
    return raw;
  }

  function eventLine(event) {
    const value = event && typeof event === 'object' ? (event.line ?? event.lineNumber ?? event.lineno ?? event.location?.line) : undefined;
    const line = Number(value);
    return Number.isInteger(line) && line > 0 ? line : null;
  }

  function eventVariables(event) {
    const values = event && (event.variables || event.locals || event.environment || event.vars || event.state);
    return values && typeof values === 'object' && !Array.isArray(values) ? values : {};
  }

  function valueText(value) {
    if (value === undefined) return 'Unknown';
    if (typeof value === 'string') return `"${value}"`;
    try {
      const json = JSON.stringify(value);
      return json === undefined ? String(value) : json;
    } catch (_) {
      return String(value);
    }
  }

  function sourceLineForEvent(event) {
    const line = eventLine(event);
    const source = state.executedSource || ($('code-editor') && $('code-editor').value) || '';
    if (Number.isInteger(line) && line > 0) {
      const sourceLines = source.split('\n');
      if (line <= sourceLines.length) return sourceLines[line - 1];
    }
    if (typeof event.source_code === 'string') return event.source_code;
    if (typeof event.source === 'string') return event.source;
    if (typeof event.code === 'string') return event.code;
    return '';
  }

  function changedVariables(index) {
    const current = eventVariables(state.trace[index]);
    const previous = index > 0 ? eventVariables(state.trace[index - 1]) : {};
    const names = new Set([...Object.keys(previous), ...Object.keys(current)]);
    return Array.from(names).filter((name) => valueText(previous[name]) !== valueText(current[name])).map((name) => ({
      name,
      before: Object.prototype.hasOwnProperty.call(previous, name) ? previous[name] : undefined,
      after: Object.prototype.hasOwnProperty.call(current, name) ? current[name] : undefined,
      removed: Object.prototype.hasOwnProperty.call(previous, name) && !Object.prototype.hasOwnProperty.call(current, name)
    }));
  }

  function eventExplanation(event, changes) {
    const type = eventType(event).toLowerCase();
    const functionName = event && (event.function || event.name);
    const line = eventLine(event);
    const location = Number.isInteger(line) ? ` on line ${line}` : '';
    if (type === 'call') return `Entered ${functionName ? `function ${functionName}` : 'a function'}${location}.`;
    if (type === 'return') {
      const returned = event && (event.return_value ?? event.returnValue ?? event.value);
      return returned === undefined ? `Returned from ${functionName ? `function ${functionName}` : 'the current function'}${location}. The returned value is unknown because the event did not include it.` : `Returned ${valueText(returned)} from ${functionName ? `function ${functionName}` : 'the current function'}${location}.`;
    }
    if (type === 'exception' || type === 'error') {
      const detail = event && (event.exception ?? event.error ?? event.message);
      return `An exception occurred${location}${detail ? `: ${detail}` : '.'}`;
    }
    if (type === 'stdout' || type === 'output') {
      const output = event && (event.output ?? event.value ?? event.text);
      return output === undefined ? `The program wrote output${location}; the event did not include its text.` : `The program wrote ${valueText(output)}${location}.`;
    }
    if (type === 'stderr') {
      const output = event && (event.output ?? event.value ?? event.text ?? event.error);
      return output === undefined ? `The program wrote an error stream event${location}; its text is unknown.` : `The program wrote ${valueText(output)} to the error stream${location}.`;
    }
    if (type === 'line') {
      if (changes.length) return `Executed the source line${location}; ${changes.map((change) => change.removed ? `${change.name} was removed` : `${change.name} changed to ${valueText(change.after)}`).join(', ')}.`;
      return `Executed the source line${location}; no variable changes were recorded for this event.`;
    }
    return `Unknown behavior for event type “${eventType(event)}”. The execution service returned no explanation for this event.`;
  }

  function renderTrace(result, source) {
    stopPlayback();
    const returnedEvents = Array.isArray(result && result.events) && result.events.length
      ? result.events
      : Array.isArray(result && result.trace) ? result.trace
        : Array.isArray(result && result.events) ? result.events : [];
    const events = returnedEvents.map((event) => event && typeof event === 'object' ? event : { type: 'unknown', value: event });
    state.trace = events;
    state.lastResult = result || null;
    state.executedSource = typeof source === 'string' ? source : state.executedSource;
    const flow = $('flow-map');
    if (flow && events.length) {
      flow.innerHTML = `<div class="flow-line"></div>${events.map((event, index) => {
        const label = eventLabel(event, index);
        return `<button class="flow-step${index === 0 ? ' active' : ''}" data-step="${index}" data-event-type="${escapeHtml(label.type)}" aria-label="Trace step ${index + 1}, ${escapeHtml(label.type)}"><b>${String(index + 1).padStart(2, '0')}</b><span>${escapeHtml(label.line)}</span><small>${escapeHtml(label.type)}</small></button>`;
      }).join('')}`;
      flow.dataset.totalSteps = String(events.length);
    } else if (flow) {
      flow.innerHTML = '<div class="flow-empty">No execution events returned. Behavior is unknown for this run.</div>';
      flow.dataset.totalSteps = '0';
    }
    state.step = events.length ? 0 : 0;
    updateTimeline();
  }

  function renderSelectedEvent() {
    const event = state.trace[state.step];
    if (!event) {
      state.activeLine = null;
      updateLineNumbers();
      setText('event-title', 'No event selected');
      setText('event-type', 'Unknown');
      const emptyEventTypeBadge = $('event-type');
      if (emptyEventTypeBadge) emptyEventTypeBadge.dataset.eventType = 'unknown';
      setText('event-meta', state.trace.length ? 'Unknown event selection.' : 'No events were returned by the execution service.');
      setText('event-source', 'Unknown source');
      setText('event-explanation', 'Unknown behavior — no execution event is selected.');
      renderVariables(state.lastResult && state.lastResult.variables ? state.lastResult.variables : {});
      renderChangedVariables([]);
      renderStack(state.lastResult && state.lastResult.call_stack);
      renderEventOutput(null);
      return;
    }
    const changes = changedVariables(state.step);
    const type = eventType(event);
    const line = eventLine(event);
    if (Number.isInteger(line) && line > 0) focusSourceLine(line);
    else {
      state.activeLine = null;
      updateLineNumbers();
    }
    setText('event-title', Number.isInteger(line) ? `Step ${state.step + 1} · line ${line}` : `Step ${state.step + 1}`);
    setText('event-type', type);
    const eventTypeBadge = $('event-type');
    if (eventTypeBadge) eventTypeBadge.dataset.eventType = type;
    setText('event-meta', `Sequence ${event.sequence ?? state.step} · ${type}${Number.isInteger(line) ? ` · line ${line}` : ''}`);
    setText('event-source', sourceLineForEvent(event) || 'Unknown source line');
    setText('event-explanation', eventExplanation(event, changes));
    renderVariables(eventVariables(event));
    renderChangedVariables(changes);
    renderStack(event.call_stack || event.stack);
    renderEventOutput(event);
    if (line) setText('run-status', `Viewing line ${line} · ${type}`);
    animateSelectedStep();
  }

  function animateSelectedStep() {
    const token = ++state.animationToken;
    if (state.animationFrame) window.cancelAnimationFrame(state.animationFrame);
    const elements = [
      document.querySelector('.trace-panel'),
      $('event-title'),
      $('event-type'),
      $('event-source'),
      $('event-explanation'),
      $('variables-list'),
      $('changed-variables'),
      $('stack-list'),
      $('event-output')
    ].filter(Boolean);
    elements.push(...all('.flow-step.active'), ...all('#line-numbers span.active'), ...all('#variables-list .data-row'), ...all('#changed-variables .data-row'), ...all('#stack-list .stack-row'), ...all('#event-output .console-line'));
    elements.forEach((element) => element.classList.remove('step-animate'));
    state.animationFrame = window.requestAnimationFrame(() => {
      if (token !== state.animationToken) return;
      elements.forEach((element) => element.classList.add('step-animate'));
      window.setTimeout(() => {
        if (token !== state.animationToken) return;
        elements.forEach((element) => element.classList.remove('step-animate'));
      }, 520);
    });
    const panel = document.querySelector('.trace-panel');
    if (panel) {
      panel.dataset.activeStep = state.trace.length ? String(state.step) : '';
      panel.dataset.eventType = state.trace.length ? eventType(state.trace[state.step]) : 'unknown';
    }
  }

  function renderChangedVariables(changes) {
    const list = $('changed-variables');
    if (!list) return;
    list.innerHTML = changes.length ? changes.map((change) => `<div class="data-row changed-row"><span class="data-type">${change.removed ? 'removed' : 'changed'}</span><b>${escapeHtml(change.name)}</b><code>${change.removed ? `${escapeHtml(valueText(change.before))} → Unknown` : `${escapeHtml(valueText(change.before))} → ${escapeHtml(valueText(change.after))}`}</code></div>`).join('') : '<div class="data-row empty-row"><span class="data-type">—</span><b>None recorded</b><code>no changes in this event</code></div>';
  }

  function renderStack(callStack) {
    const stack = $('stack-list');
    const stackState = $('stack-state');
    if (stack) {
      const frames = Array.isArray(callStack) ? callStack : [];
      if (stackState) stackState.textContent = frames.length ? `${frames.length} frame${frames.length === 1 ? '' : 's'}` : 'Unknown';
      stack.innerHTML = frames.length ? frames.slice().reverse().map((frame, index) => {
        const name = typeof frame === 'string' ? frame : frame && (frame.function || frame.name || frame.label);
        const frameLine = typeof frame === 'object' && frame ? frame.line : undefined;
        return `<div class="stack-row${index === 0 ? ' current' : ''}"><span class="stack-icon">↳</span><div><b>${escapeHtml(name || 'Unknown')}</b><small>${frameLine ? `line ${escapeHtml(frameLine)}` : 'line Unknown'}</small></div></div>`;
      }).join('') : '<div class="stack-row current"><span class="stack-icon">↳</span><div><b>Unknown</b><small>No call-stack data returned.</small></div></div>';
    }
  }

  function renderEventOutput(event) {
    const output = $('event-output');
    const status = $('event-output-status');
    if (!output) return;
    const values = event && (event.output ?? event.outputs ?? event.stdout ?? (eventType(event) === 'stderr' ? event.stderr : undefined));
    const lines = Array.isArray(values) ? values : values === undefined || values === null ? [] : String(values).split('\n').filter(Boolean);
    if (status) status.textContent = lines.length ? 'Recorded' : 'Unknown';
    output.innerHTML = lines.length ? lines.map((line) => `<div class="console-line"><span>›</span><span>${escapeHtml(line)}</span></div>`).join('') : '<div class="console-line muted-line"><span>›</span><span>No output recorded for this event.</span></div>';
  }

  function parseLiteral(value, variables) {
    const trimmed = value.trim();
    if ((trimmed.startsWith('"') && trimmed.endsWith('"')) || (trimmed.startsWith("'") && trimmed.endsWith("'"))) return trimmed.slice(1, -1);
    if (/^-?\d+(\.\d+)?$/.test(trimmed)) return Number(trimmed);
    if (trimmed === 'True') return true;
    if (trimmed === 'False') return false;
    if (trimmed === 'None') return null;
    if (variables[trimmed] !== undefined) return variables[trimmed];
    return trimmed;
  }

  function offlinePython(source) {
    const variables = {};
    const output = [];
    const diagnostics = [];
    const events = [];
    const lines = source.split('\n');
    const unsupported = lines.find((rawLine) => /^(for|while|if|elif|else|try|with|class|async|await|match|case)\b/.test(rawLine.trim()));
    if (unsupported) {
      return {
        output: [], variables: {}, events: [], lines: lines.length, status: 'unsupported',
        diagnostics: [{ title: 'Offline subset boundary', detail: 'This construct needs the CodeFlow execution service. No runtime values were invented.' }]
      };
    }
    const functions = {};
    lines.forEach((line, index) => {
      const functionMatch = line.match(/^def\s+(\w+)\(([^)]*)\):/);
      if (functionMatch) functions[functionMatch[1]] = { params: functionMatch[2].split(',').map((param) => param.trim()).filter(Boolean), line: index + 1 };
    });
    const interpolate = (value) => value.replace(/\{(\w+)\}/g, (_, key) => variables[key] === undefined ? '' : variables[key]);
    lines.forEach((rawLine, index) => {
      const line = rawLine.trim();
      if (!line || line.startsWith('#') || /^def\s/.test(line) || /^(if|return)\b/.test(line) || rawLine.startsWith('    ')) return;
      const assignment = line.match(/^(\w+)\s*=\s*(.+)$/);
      if (assignment) {
        const call = assignment[2].match(/^(\w+)\((.*)\)$/);
        if (call && functions[call[1]]) {
          const arg = parseLiteral(call[2], variables);
          const functionName = call[1];
          const messageLine = lines.find((candidate) => candidate.trim().startsWith('message ='));
          const template = messageLine ? messageLine.trim().replace(/^message\s*=\s*/, '') : '""';
          const unquoted = template.replace(/^f?["']|["']$/g, '');
          variables[assignment[1]] = interpolate(unquoted.replace(/\{\w+\}/g, `{${functions[functionName].params[0] || 'value'}}`).replace(new RegExp(`\\{${functions[functionName].params[0] || 'value'}\\}`, 'g'), String(arg)));
        } else variables[assignment[1]] = parseLiteral(assignment[2], variables);
        events.push({ sequence: events.length, type: 'line', line: index + 1, variables: { ...variables }, call_stack: [] });
      }
      const printMatch = line.match(/^print\((.*)\)$/);
      if (printMatch) {
        const value = parseLiteral(printMatch[1], variables);
        output.push(String(value));
        events.push({ sequence: events.length, type: 'stdout', line: index + 1, output: String(value), variables: { ...variables }, call_stack: [] });
      }
      if (/^(\w+)\s*=/.test(line) && !assignment) diagnostics.push({ title: `Could not parse line ${index + 1}`, detail: 'Use a simple assignment in offline mode.' });
    });
    return { output, variables, diagnostics, events, lines: lines.length, status: diagnostics.length ? 'error' : 'completed' };
  }

  async function callApi(endpoint, payload) {
    try {
      const response = await fetch(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      if (!response.ok && response.status >= 400 && response.status < 500) {
        // The server answered and rejected the request (for example "code too large"): show
        // its reason instead of pretending the API is offline and running the offline subset.
        let detail = `HTTP ${response.status}`;
        try { const body = await response.json(); if (body && body.error) detail = String(body.error); } catch (_) { /* keep the status text */ }
        setApiStatus(true);
        const rejected = { title: 'Request rejected', detail };
        return { status: 'error', valid: false, error: detail, message: detail, errors: [rejected], diagnostics: [rejected], output: [], variables: {}, events: [], call_stack: [] };
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setApiStatus(true);
      return await response.json();
    } catch (error) {
      setApiStatus(false);
      return null;
    }
  }

  async function runCode() {
    const source = $('code-editor').value;
    setText('run-status', 'Running…');
    setText('run-time', 'now');
    const result = await callApi('/api/execute', { language: state.language, code: source });
    if (result) {
      const output = Array.isArray(result.output) ? result.output.slice() : result.stdout ? String(result.stdout).split('\n').filter(Boolean) : [];
      if (result.stderr) output.push(...String(result.stderr).split('\n').filter(Boolean).map((line) => `stderr: ${line}`));
      renderConsole(output, false);
      renderVariables(result.variables || {});
      renderTrace(result, source);
      const diagnostics = result.diagnostics || result.errors || [];
      const passed = result.status === 'completed' && diagnostics.length === 0;
      renderDiagnostics(diagnostics, passed);
      setText('run-status', passed ? 'Execution complete' : `Execution stopped · ${result.status || 'issues found'}`);
      if (result.limits && result.limits.events_truncated) {
        const shown = Array.isArray(state.trace) ? state.trace.length : 0;
        setText('run-status', `${$('run-status') ? $('run-status').textContent : 'Execution complete'} · timeline limited to the first ${shown} steps`);
        showToast(`Long run: the timeline shows only the first ${shown} steps (the program itself ran to the end).`);
      }
    } else if (state.language === 'python') {
      const fallback = offlinePython(source);
      renderConsole(fallback.output, true);
      renderVariables(fallback.variables);
      renderDiagnostics(fallback.diagnostics, fallback.diagnostics.length === 0);
      renderTrace(fallback, source);
      setText('run-status', fallback.status === 'unsupported' ? 'Not executed · offline subset boundary' : 'Complete · offline fallback');
    } else {
      renderConsole(['API unavailable. Switch to Python for the offline fallback.'], true);
      renderTrace({ status: 'error', events: [], variables: {}, call_stack: [] }, source);
      renderVariables({});
      renderDiagnostics([{ title: 'Execution service unavailable', detail: 'The JavaScript offline fallback is not enabled.' }], false);
      setText('run-status', 'Could not run');
    }
  }

  async function analyzeCode() {
    const source = $('code-editor').value;
    setText('run-status', 'Analyzing…');
    const result = await callApi('/api/ai', { ...aiContext('Analyze this code and its current execution evidence.'), mode: 'analyze' });
    if (!result) {
      const fallback = offlinePython(source);
      renderDiagnostics(fallback.diagnostics, fallback.diagnostics.length === 0);
      setText('run-status', 'Analysis complete · offline');
      showToast('API unavailable — showing local diagnostics.');
      return;
    }
    if (result.status === 'ok' && typeof (result.answer || result.response) === 'string') {
      setText('ai-response', result.answer || result.response);
      renderAiProposal(result.proposed_code);
      setAiProviderStatus('remote', result.proposed_code ? 'Remote AI · analysis and proposed code received' : 'Remote AI · analysis received');
      setText('run-status', 'Analysis complete');
      showToast('Analysis received from the remote provider.');
      return;
    }
    renderAiProposal(null);
    if (result.status === 'not-configured') {
      setText('ai-response', result.message || 'AI is not configured. Set CODEFLOW_AI_API_KEY on the backend.');
      setAiProviderStatus('notConfigured', 'Not configured · no remote analysis was generated');
      setText('run-status', 'Analysis unavailable · AI not configured');
    } else {
      setText('ai-response', result.message || 'The remote AI provider returned an analysis error.');
      setAiProviderStatus('error', 'Remote AI · analysis error');
      setText('run-status', 'Analysis unavailable · provider error');
    }
  }

  async function verifyCode() {
    const source = $('code-editor').value;
    setText('run-status', 'Verifying…');
    const result = await callApi('/api/verify', { language: state.language, code: source });
    if (result) {
      const passed = result.valid !== false && result.passed !== false;
      renderDiagnostics(passed ? [] : (result.errors || [{ title: 'Verification failed', detail: 'Review the reported issue.' }]), passed);
      setText('run-status', passed ? 'Verified' : 'Verification found issues');
      showToast(passed ? 'Verification passed.' : 'Verification found an issue.');
    } else {
      const fallback = offlinePython(source);
      renderDiagnostics(fallback.diagnostics, fallback.diagnostics.length === 0);
      setText('run-status', fallback.diagnostics.length ? 'Verification found issues · offline' : 'Verified · offline');
      showToast('API unavailable — verified with local checks.');
    }
  }

  function updateTimeline() {
    const steps = all('.flow-step');
    if (!steps.length) {
      setText('step-count', '0 / 0');
      const bar = $('timeline-progress');
      const thumb = $('timeline-thumb');
      if (bar) bar.style.width = '0%';
      if (thumb) thumb.style.left = '0%';
      renderSelectedEvent();
      return;
    }
    steps.forEach((step, index) => {
      const active = index === state.step;
      step.classList.toggle('active', active);
      step.setAttribute('aria-current', active ? 'step' : 'false');
    });
    const progress = state.step / (steps.length - 1 || 1) * 100;
    const bar = $('timeline-progress');
    const thumb = $('timeline-thumb');
    if (bar) bar.style.width = `${progress}%`;
    if (thumb) thumb.style.left = `${progress}%`;
    setText('step-count', `${state.step + 1} / ${steps.length}`);
    const stepCount = $('step-count');
    if (stepCount) {
      stepCount.dataset.activeStep = String(state.step + 1);
      stepCount.dataset.totalSteps = String(steps.length);
      stepCount.setAttribute('aria-label', `Step ${state.step + 1} of ${steps.length}`);
    }
    const flow = $('flow-map');
    if (flow) flow.dataset.activeStep = String(state.step);
    renderSelectedEvent();
  }

  function selectTimelineStep(step, playSound = false, source = 'manual') {
    if (source !== 'playback') stopPlayback();
    const length = all('.flow-step').length;
    if (!length) return;
    const nextStep = Math.max(0, Math.min(length - 1, step));
    const changed = nextStep !== state.step;
    state.step = nextStep;
    updateTimeline();
    if (playSound && (changed || state.trace[state.step])) playStepSound(state.trace[state.step]);
  }

  function moveTimeline(delta, source = 'manual') {
    const length = all('.flow-step').length;
    if (!length) return;
    selectTimelineStep(state.step + delta, true, source);
    setText('run-status', `Viewing step ${state.step + 1}`);
  }

  function stopPlayback(status) {
    state.playing = false;
    state.playbackToken += 1;
    window.clearInterval(state.timer);
    state.timer = null;
    const button = $('play-button');
    if (button) {
      button.textContent = '▶';
      button.setAttribute('aria-label', 'Play timeline');
      button.classList.remove('is-playing');
    }
    if (status) setText('run-status', status);
  }

  function togglePlayback() {
    if (!all('.flow-step').length) {
      showToast('No execution events are available to play.');
      return;
    }
    if (state.playing) {
      stopPlayback('Playback paused');
      return;
    }
    if (state.step >= all('.flow-step').length - 1) selectTimelineStep(0, false);
    state.playing = true;
    const token = ++state.playbackToken;
    const button = $('play-button');
    if (button) {
      button.textContent = 'Ⅱ';
      button.setAttribute('aria-label', 'Pause timeline');
      button.classList.add('is-playing');
    }
    state.timer = window.setInterval(() => {
      if (!state.playing || token !== state.playbackToken) return;
      const length = all('.flow-step').length;
      if (!length || state.step >= length - 1) {
        stopPlayback('Playback complete');
        return;
      }
      moveTimeline(1, 'playback');
    }, 720);
  }

  function importCode(file) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => { $('code-editor').value = String(reader.result || ''); updateLineNumbers(); updateCursor(); saveSnapshot(); showToast(`Imported ${file.name}.`); };
    reader.onerror = () => showToast('Could not read that file.');
    reader.readAsText(file);
  }

  function exportCode() {
    const blob = new Blob([$('code-editor').value], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.download = state.language === 'python' ? 'codeflow-program.py' : 'codeflow-program.js';
    document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
    showToast('Code exported.');
  }

  async function askAi(prompt) {
    if (!prompt) return;
    setText('ai-response', 'Thinking about your code…');
    setAiProviderStatus('notConfigured', 'Sending code, diagnostics, selected event, and prompt to /api/ai');
    renderAiProposal(null);
    const result = await callApi('/api/ai', aiContext(prompt));
    if (!result) {
      setText('ai-response', localAiFallback(prompt));
      setAiProviderStatus('local', 'Local fallback · the backend could not be reached');
      return;
    }
    if (result.status === 'ok' && typeof (result.answer || result.response) === 'string') {
      setText('ai-response', result.answer || result.response);
      renderAiProposal(result.proposed_code);
      setAiProviderStatus('remote', result.proposed_code ? 'Remote AI · answer and proposed code received' : 'Remote AI · answer received');
      return;
    }
    renderAiProposal(null);
    if (result.status === 'not-configured') {
      setText('ai-response', result.message || 'AI is not configured. Set CODEFLOW_AI_API_KEY on the backend.');
      setAiProviderStatus('notConfigured', 'Not configured · no remote answer was generated');
    } else {
      const diagnostic = Array.isArray(result.diagnostics) && result.diagnostics[0];
      setText('ai-response', result.message || 'The remote AI provider returned an error. Review the provider diagnostics.');
      setAiProviderStatus('error', `Remote AI · provider error${diagnostic && diagnostic.detail ? `: ${diagnostic.detail}` : ''}`);
    }
  }

  function loadExample(name) {
    const examples = {
      recursion: 'def countdown(n):\n    if n <= 0:\n        return "go!"\n    return countdown(n - 1)\n\nresult = countdown(3)\nprint(result)',
      loops: 'total = 0\nfor number in [1, 2, 3, 4]:\n    total = total + number\nprint(total)',
      debug: 'name = "Ada"\ngreeting = "Hello, " + name\nprint(greeting)'
    };
    const editor = $('code-editor');
    if (editor && examples[name]) { editor.value = examples[name]; state.language = 'python'; $('language-select').value = 'python'; updateLineNumbers(); updateCursor(); saveSnapshot(); showToast(`${name[0].toUpperCase()}${name.slice(1)} example loaded.`); }
  }

  function bindEvents() {
    const editor = $('code-editor');
    editor.addEventListener('input', () => { updateLineNumbers(); updateCursor(); saveSnapshot(); });
    ['keyup', 'click', 'select'].forEach((event) => editor.addEventListener(event, updateCursor));
    $('run-button').addEventListener('click', runCode);
    $('analyze-button').addEventListener('click', analyzeCode);
    $('verify-button').addEventListener('click', verifyCode);
    $('previous-step').addEventListener('click', () => moveTimeline(-1));
    $('next-step').addEventListener('click', () => moveTimeline(1));
    $('play-button').addEventListener('click', togglePlayback);
    $('sound-toggle').addEventListener('click', toggleSound);
    $('reset-timeline').addEventListener('click', () => { selectTimelineStep(0, true); setText('run-status', 'Waiting for your code'); });
    $('flow-map').addEventListener('click', (event) => {
      const step = event.target.closest('.flow-step');
      if (!step) return;
      selectTimelineStep(Number(step.dataset.step) || 0, true);
    });
    $('diagnostics-output').addEventListener('click', (event) => {
      const item = event.target.closest('[data-line]');
      if (item) focusSourceLine(item.dataset.line);
    });
    $('diagnostics-output').addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      const item = event.target.closest('[data-line]');
      if (!item) return;
      event.preventDefault();
      focusSourceLine(item.dataset.line);
    });
    $('clear-console').addEventListener('click', () => renderConsole([]));
    $('find-button').addEventListener('click', () => { $('find-bar').classList.remove('hidden'); $('find-input').focus(); });
    $('close-find').addEventListener('click', () => $('find-bar').classList.add('hidden'));
    $('replace-button').addEventListener('click', () => replaceText(false));
    $('replace-all-button').addEventListener('click', () => replaceText(true));
    $('theme-toggle').addEventListener('click', () => { document.body.classList.toggle('light'); showToast(document.body.classList.contains('light') ? 'Light theme enabled.' : 'Dark theme enabled.'); });
    $('import-button').addEventListener('click', () => $('file-input').click());
    $('file-input').addEventListener('change', (event) => importCode(event.target.files[0]));
    $('export-button').addEventListener('click', exportCode);
    $('restore-button').addEventListener('click', () => { editor.value = state.snapshot; updateLineNumbers(); updateCursor(); showToast('Last saved version restored.'); });
    $('undo-button').addEventListener('click', () => { if (state.history.length > 1) { state.history.pop(); editor.value = state.history[state.history.length - 1]; updateLineNumbers(); updateCursor(); showToast('Undid the last edit.'); } else showToast('Nothing to undo yet.'); });
    $('language-select').addEventListener('change', (event) => { state.language = event.target.value; $('file-name').textContent = event.target.value === 'python' ? 'main.py' : 'main.js'; showToast(`${event.target.options[event.target.selectedIndex].text} selected.`); });
    $('ai-button').addEventListener('click', () => { const input = $('ai-input'); askAi(input.value.trim()); input.value = ''; });
    $('ai-input').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); $('ai-button').click(); } });
    all('.chip').forEach((chip) => chip.addEventListener('click', () => askAi(chip.dataset.prompt)));
    all('[data-example]').forEach((link) => link.addEventListener('click', () => loadExample(link.dataset.example)));
    $('challenge-button').addEventListener('click', () => { loadExample('debug'); document.querySelector('#visualizer').scrollIntoView({ behavior: 'smooth' }); showToast('Challenge loaded — find the output mismatch.'); });
    $('mobile-menu').addEventListener('click', () => { const nav = document.querySelector('.main-nav'); const open = nav.classList.toggle('mobile-open'); $('mobile-menu').setAttribute('aria-expanded', String(open)); });
    all('.nav-link').forEach((link) => link.addEventListener('click', () => document.querySelector('.main-nav').classList.remove('mobile-open')));
  }

  function replaceText(allMatches) {
    const editor = $('code-editor');
    const find = $('find-input').value;
    if (!find) return showToast('Enter text to find first.');
    const replacement = $('replace-input').value;
    if (allMatches) editor.value = editor.value.split(find).join(replacement);
    else { const index = editor.value.indexOf(find); if (index >= 0) editor.setRangeText(replacement, index, index + find.length, 'select'); }
    updateLineNumbers(); updateCursor(); saveSnapshot(); showToast(allMatches ? 'All matches replaced.' : 'First match replaced.');
  }

  function start() {
    try {
      const editor = $('code-editor');
      let saved = '';
      try { saved = localStorage.getItem('codeflow.source') || ''; } catch (_) { /* persistence is optional */ }
      if (editor && saved) editor.value = saved;
      state.source = editor ? editor.value : '';
      state.snapshot = state.source;
      state.history = [state.source];
      updateLineNumbers(); updateCursor(); updateTimeline(); bindEvents(); configureSoundSupport(); refreshHealth();
    } catch (error) {
      // Keep the page usable even if a browser extension changes the DOM.
      setText('run-status', 'Ready — some optional controls are unavailable');
      showToast('CodeFlow loaded with limited controls.');
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
