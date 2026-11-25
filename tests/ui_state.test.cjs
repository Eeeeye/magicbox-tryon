const test = require('node:test');
const assert = require('node:assert/strict');

const {
  createStore,
  normalizeTask,
  filterTasks,
  abortableDelay,
  appendServiceSample,
} = require('../web/ui-state.js');

const HISTORY_KEY = 'tryon.recentTasks.v2';
const ids = {
  queued: '00000000-0000-4000-8000-000000000001',
  running: '00000000-0000-4000-8000-000000000002',
  succeeded: '00000000-0000-4000-8000-000000000003',
  failed: '00000000-0000-4000-8000-000000000004',
};

function task(taskId, status, createdAt, extra = {}) {
  return {
    taskId,
    status,
    createdAt,
    updatedAt: createdAt,
    mode: 'mock',
    durationMs: status === 'QUEUED' ? null : 12,
    inputs: {
      person: { width: 2, height: 3 },
      garment: { width: 4, height: 5 },
    },
    ...extra,
  };
}

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    values,
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, String(value)); },
  };
}

test('malformed storage is ignored and valid task records are retained', () => {
  const storage = memoryStorage({ [HISTORY_KEY]: '{not-json' });
  const store = createStore(() => storage);
  assert.deepEqual(store.tasks(), []);

  storage.values.set(HISTORY_KEY, JSON.stringify([
    null,
    {},
    task('not-a-uuid', 'SUCCEEDED', '2026-10-01T00:00:00.000Z'),
    task(ids.succeeded, 'SUCCEEDED', '2026-10-01T00:00:00.000Z'),
  ]));
  const refreshedStore = createStore(() => storage);
  assert.deepEqual(refreshedStore.tasks().map(({ taskId }) => taskId), [ids.succeeded]);

  storage.values.set(HISTORY_KEY, JSON.stringify({ [ids.succeeded]: task(ids.succeeded, 'SUCCEEDED', '2026-10-01T00:00:00.000Z') }));
  const nonArrayStore = createStore(() => storage);
  assert.deepEqual(nonArrayStore.tasks(), []);
});

test('legacy flat image metadata is normalized into the current inputs shape', () => {
  const legacy = task(ids.succeeded, 'SUCCEEDED', '2026-10-01T00:00:00.000Z');
  legacy.inputs = undefined;
  legacy.person = { width: 11, height: 13 };
  legacy.garment = { width: 17, height: 19 };
  const normalized = normalizeTask(legacy);

  assert.equal(normalized.taskId, ids.succeeded);
  assert.deepEqual(normalized.inputs, {
    person: { width: 11, height: 13 },
    garment: { width: 17, height: 19 },
  });
  assert.deepEqual(normalized.timeline, {
    queuedAt: '2026-10-01T00:00:00.000Z', runningAt: null, completedAt: null,
  });
  assert.equal(normalized.status, 'SUCCEEDED');
});

test('refresh keeps equal-createdAt records in their stored order', () => {
  const sameTime = '2026-10-02T12:00:00.000Z';
  const storage = memoryStorage({
    [HISTORY_KEY]: JSON.stringify([
      task(ids.queued, 'QUEUED', sameTime),
      task(ids.running, 'RUNNING', sameTime),
      task(ids.succeeded, 'SUCCEEDED', '2026-10-01T12:00:00.000Z'),
    ]),
  });
  const store = createStore(() => storage);
  const expected = [ids.queued, ids.running, ids.succeeded];
  assert.deepEqual(store.tasks().map(({ taskId }) => taskId), expected);
  assert.deepEqual(store.tasks().map(({ taskId }) => taskId), expected);
});

test('status filters return the expected counts, including ACTIVE', () => {
  const tasks = [
    task(ids.queued, 'QUEUED', '2026-10-04T00:00:00.000Z'),
    task(ids.running, 'RUNNING', '2026-10-03T00:00:00.000Z'),
    task(ids.succeeded, 'SUCCEEDED', '2026-10-02T00:00:00.000Z'),
    task(ids.failed, 'FAILED', '2026-10-01T00:00:00.000Z'),
  ];
  assert.equal(filterTasks(tasks, 'ALL').length, 4);
  assert.equal(filterTasks(tasks, 'ACTIVE').length, 2);
  assert.equal(filterTasks(tasks, 'QUEUED').length, 1);
  assert.equal(filterTasks(tasks, 'RUNNING').length, 1);
  assert.equal(filterTasks(tasks, 'SUCCEEDED').length, 1);
  assert.equal(filterTasks(tasks, 'FAILED').length, 1);
  assert.deepEqual(filterTasks(tasks, 'ACTIVE').map(({ status }) => status), ['QUEUED', 'RUNNING']);
});

test('get failure falls back to memory and reports unavailability once', () => {
  let notices = 0;
  const store = createStore(() => {
    throw new Error('storage blocked');
  }, () => { notices += 1; });

  assert.deepEqual(store.tasks(), []);
  assert.equal(store.save(task(ids.running, 'RUNNING', '2026-10-03T00:00:00.000Z')), true);
  assert.equal(store.save(task(ids.running, 'RUNNING', '2026-10-03T00:00:00.000Z')), false);
  assert.deepEqual(store.tasks().map(({ taskId }) => taskId), [ids.running]);
  assert.equal(notices, 1);
});

test('set failure keeps saved records in memory and clear works in fallback', () => {
  const storage = memoryStorage();
  let writes = 0;
  storage.setItem = () => {
    writes += 1;
    throw new Error('quota exceeded');
  };
  let notices = 0;
  const store = createStore(() => storage, () => { notices += 1; });

  assert.equal(store.save(task(ids.failed, 'FAILED', '2026-10-01T00:00:00.000Z', { error: 'BROKEN' })), true);
  assert.equal(writes, 1);
  assert.equal(notices, 1);
  assert.deepEqual(store.tasks().map(({ taskId }) => taskId), [ids.failed]);
  store.clear();
  assert.deepEqual(store.tasks(), []);
});

test('abortableDelay settles promptly when aborted', async () => {
  const controller = new AbortController();
  const reason = new Error('cancelled');
  const pending = abortableDelay(1000, controller.signal);
  controller.abort(reason);
  await assert.rejects(pending, error => error === reason);
});

test('abortableDelay rejects immediately for an already aborted signal', async () => {
  const controller = new AbortController();
  const reason = new Error('already cancelled');
  controller.abort(reason);
  await assert.rejects(abortableDelay(1000, controller.signal), error => error === reason);
});

test('service samples distinguish idle, missing data and recovery without storing response extras', () => {
  const at = '2026-10-04T08:00:00.000Z';
  const busy = { status: 'UP', queueDepth: 3, activeWorkers: 1, taskCount: 9, extra: 'not-retained' };
  let samples = appendServiceSample([], busy, at);
  assert.deepEqual(samples[0], { observedAt: at, available: true, queueDepth: 3, activeWorkers: 1, taskCount: 9 });
  samples = appendServiceSample(samples, null, at);
  assert.equal(samples[0].queueDepth, 3);
  assert.equal(samples[1].available, false);
  assert.equal(samples[1].queueDepth, null);
  samples = appendServiceSample(samples, { ...busy, queueDepth: 0, activeWorkers: 0 }, at);
  assert.equal(samples[2].available, true);
  assert.equal(samples[2].queueDepth, 0);
});

test('service samples bound history and reject partial, malformed or unsafe counter values', () => {
  const healthy = { status: 'UP', queueDepth: 0, activeWorkers: 0, taskCount: 0 };
  for (const invalid of [{}, { ...healthy, status: 'DOWN' }, { ...healthy, queueDepth: -1 },
    { ...healthy, queueDepth: '0' }, { ...healthy, activeWorkers: 0.5 },
    { ...healthy, taskCount: Number.MAX_SAFE_INTEGER + 1 }]) {
    const sample = appendServiceSample([], invalid)[0];
    assert.equal(sample.available, false);
    assert.equal(sample.queueDepth, null);
    assert.equal(sample.activeWorkers, null);
    assert.equal(sample.taskCount, null);
  }
  let samples = [];
  for (let index = 0; index < 10; index++) samples = appendServiceSample(samples, healthy, `reading-${index}`);
  assert.equal(samples.length, 8);
  assert.equal(samples[0].observedAt, 'reading-2');
  assert.equal(samples.at(-1).observedAt, 'reading-9');
});

test('a real image task retains its mode and garment category across reloads', () => {
  const storage = memoryStorage();
  const initial = createStore(() => storage);
  initial.save(task(ids.succeeded, 'SUCCEEDED', '2026-10-01T00:00:00.000Z', { mode: 'catvton', category: 'dress' }));
  const [record] = createStore(() => storage).tasks();
  assert.equal(record.mode, 'catvton');
  assert.equal(record.category, 'dress');
});
