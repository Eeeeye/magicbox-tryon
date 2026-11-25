/* Shared browser state rules; also loadable by the dependency-free Node tests. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TryOnState = api;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  const HISTORY_KEY = 'tryon.recentTasks.v2';
  const statuses = new Set(['QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED']);
  const uuid = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
  const date = value => typeof value === 'string' && Number.isFinite(Date.parse(value)) ? value : null;
  const positive = value => Number.isInteger(value) && value > 0;
  function imageMeta(value) {
    if (!value || !positive(value.width) || !positive(value.height)) return undefined;
    return { width: value.width, height: value.height };
  }
  function normalizeTask(value) {
    if (!value || !uuid.test(value.taskId) || !statuses.has(value.status) || !date(value.createdAt)) return null;
    const timeline = value.timeline || {};
    return {
      taskId: value.taskId, status: value.status, mode: value.mode === 'catvton' ? 'catvton' : 'mock',
      category: ['upper', 'lower', 'dress'].includes(value.category) ? value.category : 'upper',
      createdAt: value.createdAt,
      updatedAt: date(value.updatedAt),
      durationMs: Number.isFinite(value.durationMs) && value.durationMs >= 0 ? value.durationMs : null,
      error: typeof value.error === 'string' ? value.error.slice(0, 200) : '',
      timeline: {
        queuedAt: date(timeline.queuedAt) || value.createdAt,
        runningAt: date(timeline.runningAt),
        completedAt: date(timeline.completedAt)
      },
      inputs: { person: imageMeta(value.inputs?.person || value.person), garment: imageMeta(value.inputs?.garment || value.garment) }
    };
  }
  function createStore(getStorage, onUnavailable = () => {}) {
    const memory = new Map();
    let unavailable = false;
    function fallback() {
      if (!unavailable) { unavailable = true; onUnavailable(); }
    }
    function get(key) {
      if (memory.has(key)) return memory.get(key);
      try {
        const value = getStorage().getItem(key);
        memory.set(key, value);
        return value;
      } catch (_) { fallback(); return null; }
    }
    function set(key, value) {
      memory.set(key, value);
      if (unavailable) return;
      try { getStorage().setItem(key, value); } catch (_) { fallback(); }
    }
    function tasks() {
      let parsed;
      try { parsed = JSON.parse(get(HISTORY_KEY) || '[]'); } catch (_) { parsed = []; }
      if (!Array.isArray(parsed)) return [];
      const unique = new Map();
      for (const value of parsed) {
        const task = normalizeTask(value);
        if (task && !unique.has(task.taskId)) unique.set(task.taskId, task);
      }
      return [...unique.values()].sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt)).slice(0, 8);
    }
    function save(data) {
      const task = normalizeTask(data);
      if (!task) return false;
      const before = tasks();
      const next = [task, ...before.filter(item => item.taskId !== task.taskId)]
        .sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt)).slice(0, 8);
      if (JSON.stringify(next) === JSON.stringify(before)) return false;
      set(HISTORY_KEY, JSON.stringify(next));
      return true;
    }
    return { get, set, tasks, save, clear: () => set(HISTORY_KEY, '[]') };
  }
  function filterTasks(tasks, filter) {
    if (filter === 'ACTIVE') return tasks.filter(task => ['QUEUED', 'RUNNING'].includes(task.status));
    return filter === 'ALL' ? tasks : tasks.filter(task => task.status === filter);
  }
  // Missing readings are gaps, never zero load. Timestamps are browser receipt times.
  function appendServiceSample(samples, data, observedAt = new Date().toISOString()) {
    const count = value => Number.isSafeInteger(value) && value >= 0;
    const valid = data?.status === 'UP' && count(data.queueDepth)
      && count(data.activeWorkers) && count(data.taskCount);
    const sample = { observedAt, available: valid,
      queueDepth: valid ? data.queueDepth : null,
      activeWorkers: valid ? data.activeWorkers : null,
      taskCount: valid ? data.taskCount : null };
    return [...samples, sample].slice(-8);
  }
  function abortableDelay(ms, signal) {
    return new Promise((resolve, reject) => {
      const cancel = () => { clearTimeout(timer); reject(signal.reason); };
      const timer = setTimeout(() => { signal.removeEventListener('abort', cancel); resolve(); }, ms);
      if (signal.aborted) cancel();
      else signal.addEventListener('abort', cancel, { once: true });
    });
  }
  return { createStore, normalizeTask, filterTasks, abortableDelay, appendServiceSample };
});
