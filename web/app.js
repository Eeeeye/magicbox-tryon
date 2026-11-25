const form = document.querySelector('#form');
const submit = document.querySelector('#submit');
const reset = document.querySelector('#reset');
const empty = document.querySelector('#empty');
const view = document.querySelector('#statusView');
const toast = document.querySelector('#toast');
const historyList = document.querySelector('#historyList');
const themeToggle = document.querySelector('#themeToggle');
const copySummary = document.querySelector('#copySummary');
const downloadSummary = document.querySelector('#downloadSummary');
const queueDepthValue = document.querySelector('#queueDepthValue');
const workerValue = document.querySelector('#workerValue');
const taskCountValue = document.querySelector('#taskCountValue');
const queueSummary = document.querySelector('#queueSummary');
const queueTrend = document.querySelector('#queueTrend');
const refreshService = document.querySelector('#refreshService');
const inputSummary = document.querySelector('#inputSummary');
const inputSummaryTitle = document.querySelector('#inputSummaryTitle');
const inputSummaryValue = document.querySelector('#inputSummaryValue');
const inputSummaryNote = document.querySelector('#inputSummaryNote');
const fields = {
  person: document.querySelector('[data-role="person"]'),
  garment: document.querySelector('[data-role="garment"]')
};
const MAX_FILE_SIZE = 5 * 1024 * 1024;
const SUPPORTED_TYPES = ['image/png', 'image/jpeg'];
const terminalStatuses = new Set(['SUCCEEDED', 'FAILED']);
const { createStore, filterTasks, abortableDelay, appendServiceSample } = TryOnState;
const store = createStore(() => window.localStorage, () => {
  showToast('本地存储不可用；新记录仅在本次页面中保留');
});
let activeFilter = 'ALL';
let selectedTaskId = null;
let submissionInFlight = false;
let retryKey = null;
document.querySelector('#category').addEventListener('change', () => { retryKey = null; });
let observation = null;
let interactionToken = 0;
let historyEpoch = 0;
let currentTaskData = null;
let currentTaskSource = 'local-history-snapshot';
let telemetrySamples = [];
let lastServiceSampleAt = null;
const previewUrls = new Map();
const selectedFiles = { person: false, garment: false };
const inputDimensions = { person: null, garment: null };
const imageReadTokens = { person: 0, garment: 0 };

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function showToast(message, type = '') {
  toast.textContent = message;
  toast.className = `toast show ${type}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.className = 'toast'; }, 3200);
}

function syncSubmit() {
  submit.disabled = submissionInFlight || !(selectedFiles.person && selectedFiles.garment);
  submit.textContent = submissionInFlight ? '提交中…' : '开始试穿';
  submit.setAttribute('aria-busy', String(submissionInFlight));
}

function ratio(width, height) {
  const divisor = (a, b) => b ? divisor(b, a % b) : a;
  const common = divisor(width, height);
  return `${width / common}:${height / common}`;
}
function renderInputSummary() {
  const roles = [['person', '人物'], ['garment', '服装']];
  const selected = roles.filter(([role]) => selectedFiles[role]);
  inputSummary.hidden = selected.length === 0;
  if (!selected.length) return;
  const ready = selected.filter(([role]) => inputDimensions[role]);
  inputSummaryTitle.textContent = ready.length === selected.length ? '图片尺寸' : '正在读取图片尺寸…';
  inputSummaryValue.textContent = ready.map(([role, label]) => {
    const value = inputDimensions[role];
    return `${label} ${value.width} × ${value.height} · ${ratio(value.width, value.height)}`;
  }).join('  ·  ');
  inputSummaryNote.textContent = ready.length === selected.length
    ? ''
    : '图片仍在本地读取，尺寸出现后即可复核。';
}

function clearFile(role) {
  retryKey = null;
  const zone = fields[role];
  const input = zone.querySelector('input');
  const preview = zone.querySelector('.preview');
  const name = zone.querySelector('.file-name');
  imageReadTokens[role] += 1;
  inputDimensions[role] = null;
  const previousUrl = previewUrls.get(role);
  if (previousUrl) {
    URL.revokeObjectURL(previousUrl);
    previewUrls.delete(role);
  }
  input.value = '';
  preview.removeAttribute('src');
  zone.classList.remove('has-file');
  name.textContent = 'PNG / JPEG · 最大 5 MiB';
  selectedFiles[role] = false;
  renderInputSummary();
  syncSubmit();
}

function setFile(role, file) {
  retryKey = null;
  const zone = fields[role];
  const input = zone.querySelector('input');
  const preview = zone.querySelector('.preview');
  const name = zone.querySelector('.file-name');
  if (!file) { clearFile(role); return; }
  if (!SUPPORTED_TYPES.includes(file.type)) {
    clearFile(role);
    showToast('只支持 PNG 或 JPEG 图片', 'error');
    return;
  }
  if (file.size > MAX_FILE_SIZE) {
    clearFile(role);
    showToast('单张图片不能超过 5 MiB', 'error');
    return;
  }
  const previousUrl = previewUrls.get(role);
  if (previousUrl) URL.revokeObjectURL(previousUrl);
  const previewUrl = URL.createObjectURL(file);
  const readToken = ++imageReadTokens[role];
  inputDimensions[role] = null;
  previewUrls.set(role, previewUrl);
  zone.classList.add('has-file');
  name.textContent = file.name;
  selectedFiles[role] = true;
  preview.onload = () => {
    if (readToken !== imageReadTokens[role]) return;
    inputDimensions[role] = { width: preview.naturalWidth, height: preview.naturalHeight };
    renderInputSummary();
  };
  preview.onerror = () => {
    if (readToken !== imageReadTokens[role]) return;
    inputDimensions[role] = null;
    renderInputSummary();
  };
  preview.src = previewUrl;
  renderInputSummary();
  syncSubmit();
}

for (const [role, zone] of Object.entries(fields)) {
  const input = zone.querySelector('input');
  input.addEventListener('change', () => setFile(role, input.files[0]));
  for (const event of ['dragenter', 'dragover']) {
    zone.addEventListener(event, eventObject => {
      eventObject.preventDefault();
      zone.classList.add('over');
    });
  }
  for (const event of ['dragleave', 'drop']) {
    zone.addEventListener(event, eventObject => {
      eventObject.preventDefault();
      zone.classList.remove('over');
    });
  }
  zone.addEventListener('drop', eventObject => {
    const file = eventObject.dataTransfer.files[0];
    if (!file) return;
    try {
      const transfer = new DataTransfer();
      transfer.items.add(file);
      input.files = transfer.files;
      setFile(role, file);
    } catch (_) {
      showToast('请点击选择文件', 'error');
    }
  });
}

const sampleButton = document.querySelector('#loadSamples');
sampleButton.addEventListener('click', async () => {
  sampleButton.disabled = true;
  const category = document.querySelector('#category').value;
  try {
    const entries = await Promise.all(Object.keys(fields).map(async role => {
      const fileName = role === 'person'
        ? (category === 'lower' ? 'person-lower.png' : 'person.jpg')
        : (category === 'upper' ? 'garment.jpg' : `${category}.jpg`);
      const response = await fetch(`/samples/${fileName}`);
      if (!response.ok) throw new Error('样例未安装，请运行模型下载脚本');
      const image = await response.blob();
      return [role, new File([image], fileName, { type: image.type })];
    }));
    for (const [role, file] of entries) {
      const transfer = new DataTransfer();
      transfer.items.add(file);
      fields[role].querySelector('input').files = transfer.files;
      setFile(role, file);
    }
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    sampleButton.disabled = false;
  }
});

const statusLabels = { QUEUED: '排队中', RUNNING: '处理中', SUCCEEDED: '已完成', FAILED: '未完成' };
const dateFormat = new Intl.DateTimeFormat('zh-CN', {
  month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'
});
function formatDate(value) {
  return value && Number.isFinite(Date.parse(value)) ? dateFormat.format(new Date(value)) : '未记录';
}
function dimensions(value) { return value ? `${value.width} × ${value.height}` : '未记录'; }
function renderTelemetry(data) {
  telemetrySamples = appendServiceSample(telemetrySamples, data);
  const latest = telemetrySamples.at(-1);
  if (latest.available) lastServiceSampleAt = latest.observedAt;
  queueDepthValue.textContent = latest.queueDepth ?? '—';
  workerValue.textContent = latest.activeWorkers ?? '—';
  taskCountValue.textContent = latest.taskCount ?? '—';
  queueSummary.classList.toggle('unavailable', !latest.available);
  document.querySelector('#serviceMetrics').classList.toggle('unavailable', !latest.available);
  queueSummary.textContent = latest.available
    ? `读取于 ${formatDate(latest.observedAt)} · 离散读数可能错过短暂的排队高峰。`
    : `读数暂不可用${lastServiceSampleAt ? ` · 上次成功读取 ${formatDate(lastServiceSampleAt)}` : ''} · 图中保留本页历史，虚线表示读取失败。`;
  const peak = Math.max(0, ...telemetrySamples.filter(sample => sample.available).map(sample => sample.queueDepth));
  document.querySelector('#queuePeak').textContent = telemetrySamples.some(sample => sample.available) ? `样本最高 ${peak}` : '暂无有效读数';
  queueTrend.replaceChildren();
  for (const sample of telemetrySamples) {
    const bar = document.createElement('span');
    bar.className = sample.available ? (sample.queueDepth ? 'queue-bar' : 'queue-bar zero') : 'queue-bar gap';
    bar.style.height = sample.available && sample.queueDepth ? `${(sample.queueDepth / peak) * 100}%` : '2px';
    bar.title = `${formatDate(sample.observedAt)} · ${sample.available ? `队列 ${sample.queueDepth}` : '读取失败'}`;
    bar.setAttribute('aria-hidden', 'true');
    queueTrend.append(bar);
  }
  queueTrend.setAttribute('aria-label', `本页最近 ${telemetrySamples.length} 次队列读数，柱高按样本最高值缩放：${telemetrySamples.map(sample => `${formatDate(sample.observedAt)} ${sample.available ? sample.queueDepth : '读取失败'}`).join('；')}`);
}
function summaryPayload(data, source) {
  const inputs = data.inputs || {};
  const image = value => value ? {
    mimeType: value.mimeType,
    bytes: value.bytes,
    width: value.width,
    height: value.height,
    sha256: value.sha256
  } : null;
  return {
    taskId: data.taskId,
    status: data.status,
    mode: data.mode || 'mock',
    createdAt: data.createdAt || null,
    updatedAt: data.updatedAt || null,
    durationMs: data.durationMs ?? null,
    timeline: data.timeline || null,
    inputs: { person: image(inputs.person), garment: image(inputs.garment) },
    result: data.result || null,
    error: data.error || null,
    source,
    category: data.category || 'upper'
  };
}
function setTaskToolsEnabled(enabled) {
  copySummary.disabled = !enabled;
  downloadSummary.disabled = !enabled;
}
async function writeClipboard(text) {
  if (navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(text); return; } catch (_) { /* use the local fallback */ }
  }
  const textarea = document.createElement('textarea');
  const previousFocus = document.activeElement;
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.append(textarea);
  try {
    textarea.select();
    if (!document.execCommand('copy')) throw new Error('当前浏览器不支持复制');
  } finally {
    textarea.remove();
    previousFocus?.focus({ preventScroll: true });
  }
}
function downloadTaskSummary(data) {
  const payload = JSON.stringify(summaryPayload(data, currentTaskSource), null, 2);
  const blob = new Blob([payload], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `tryon-${data.taskId}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
function renderTimeline(data) {
  const timeline = { queuedAt: data.createdAt, ...(data.timeline || {}) };
  const stages = [
    ['queued', timeline.queuedAt, data.status === 'QUEUED'],
    ['running', timeline.runningAt, data.status === 'RUNNING'],
    ['completed', timeline.completedAt, false]
  ];
  for (const [stage, value, active] of stages) {
    const item = document.querySelector(`[data-stage="${stage}"]`);
    const time = document.querySelector(`#${stage}At`);
    item.classList.toggle('done', Boolean(value));
    item.classList.toggle('active', active);
    time.textContent = formatDate(value);
    if (value) time.dateTime = value;
    else time.removeAttribute('datetime');
  }
  const completedLabel = document.querySelector('#completedLabel');
  completedLabel.textContent = data.status === 'FAILED' ? '任务结束 · 未完成' : '完成任务';
}
function duration(value) {
  return value == null ? '耗时未记录' : value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(2)} s`;
}
function saveHistory(data) {
  if (store.save(data)) renderHistory();
}

function renderHistory() {
  const items = store.tasks();
  const visible = filterTasks(items, activeFilter);
  const focusedId = document.activeElement?.closest('.history-item')?.dataset.taskId;
  document.querySelector('#historyTotal').textContent = `${items.length} 次尝试`;
  document.querySelector('#historySummary').textContent = `显示 ${visible.length} / ${items.length} 条记录`;
  document.querySelector('#clearHistory').disabled = !items.length;
  for (const button of document.querySelectorAll('[data-filter]')) {
    button.setAttribute('aria-pressed', String(button.dataset.filter === activeFilter));
    button.querySelector('span').textContent = filterTasks(items, button.dataset.filter).length;
  }
  historyList.replaceChildren();
  if (!visible.length) {
    const node = document.createElement('div');
    node.className = 'history-empty';
    node.textContent = items.length ? '这个分类还没有任务，试试切换到「全部」。' : '你的第一次尝试，将从这里开始。';
    historyList.append(node);
    return;
  }
  for (const item of visible) {
    const button = document.createElement('button');
    button.className = 'history-item';
    button.type = 'button';
    button.dataset.taskId = item.taskId;
    button.setAttribute('aria-current', String(item.taskId === selectedTaskId));
    const main = document.createElement('span');
    main.className = 'history-main';
    const id = document.createElement('strong');
    id.textContent = item.taskId;
    const meta = document.createElement('span');
    meta.textContent = `人物 ${dimensions(item.inputs.person)} · 服装 ${dimensions(item.inputs.garment)}`;
    const time = document.createElement('time');
    time.dateTime = item.createdAt;
    time.textContent = formatDate(item.createdAt);
    main.append(id, meta, time);
    const side = document.createElement('span');
    side.className = 'history-side';
    const state = document.createElement('span');
    state.className = `history-status ${item.status === 'SUCCEEDED' ? 'success' : item.status === 'FAILED' ? 'fail' : 'wait'}`;
    state.textContent = statusLabels[item.status];
    const elapsed = document.createElement('span');
    elapsed.className = 'history-duration';
    elapsed.textContent = duration(item.durationMs);
    side.append(state, elapsed);
    button.append(main, side);
    button.addEventListener('click', () => loadTask(item.taskId));
    historyList.append(button);
    if (item.taskId === focusedId) button.focus({ preventScroll: true });
  }
}

for (const button of document.querySelectorAll('[data-filter]')) {
  button.addEventListener('click', () => { activeFilter = button.dataset.filter; renderHistory(); });
}

// Every request has a bounded lifetime, including reading the response body.
async function requestJson(url, options = {}, parentSignal, timeoutMs = 4000) {
  const controller = new AbortController();
  const cancel = () => controller.abort(parentSignal.reason);
  const timeout = setTimeout(() => controller.abort(new DOMException('请求超时', 'TimeoutError')), timeoutMs);
  if (parentSignal?.aborted) cancel();
  else parentSignal?.addEventListener('abort', cancel, { once: true });
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    let data;
    try { data = await response.json(); }
    catch (_) { throw new ApiError('服务响应无法读取', response.status); }
    if (!response.ok) throw new ApiError(data.error || '请求失败', response.status);
    return data;
  } finally {
    clearTimeout(timeout);
    parentSignal?.removeEventListener('abort', cancel);
  }
}

function stopObservation() {
  observation?.abort();
  observation = null;
}
function focusStatus() {
  const heading = document.querySelector('#statusHeading');
  heading.focus({ preventScroll: true });
  heading.scrollIntoView({ block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
}
function showSnapshot(id, reason) {
  const cached = store.tasks().find(item => item.taskId === id);
  if (!cached) { showToast(reason, 'error'); return; }
  render(cached, `本地记录 · ${reason}。下方为上次记录，不代表当前运行状态。`);
}
async function loadTask(id) {
  interactionToken += 1;
  selectedTaskId = id;
  renderHistory();
  // Show a labelled snapshot immediately while checking its availability.
  showSnapshot(id, '正在核对服务端状态');
  focusStatus();
  await observeTask(id);
}

async function observeTask(id, initial) {
  stopObservation();
  const controller = new AbortController();
  observation = controller;
  const { signal } = controller;
  const deadline = Date.now() + 20 * 60 * 1000;
  let failures = 0;
  if (initial) render(initial);
  try {
    while (!signal.aborted && Date.now() < deadline) {
      try {
        const data = await requestJson(`/api/tryon/tasks/${encodeURIComponent(id)}`, {}, signal,
          Math.min(4000, Math.max(1, deadline - Date.now())));
        if (signal.aborted) return;
        failures = 0;
        render(data);
        saveHistory(data);
        if (terminalStatuses.has(data.status)) return;
      } catch (error) {
        if (signal.aborted) return;
        if (error instanceof ApiError && error.status === 404) {
          showSnapshot(id, '服务端已找不到此任务');
          return;
        }
        failures += 1;
        if (failures >= 3 || (error instanceof ApiError && error.status >= 400 && error.status < 500)) {
          showSnapshot(id, '暂时无法查询服务端，可从历史记录重试');
          return;
        }
        if (failures === 1) showToast('查询暂时失败，正在重试…');
      }
      const remaining = deadline - Date.now();
      if (remaining > 0) await abortableDelay(Math.min(1200, remaining), signal);
    }
    if (!signal.aborted) showSnapshot(id, '已结束本次 20 分钟查询，可从历史记录重试');
  } catch (error) {
    if (!signal.aborted) showToast(error.message || '查询失败', 'error');
  } finally {
    if (observation === controller) observation = null;
  }
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (submissionInFlight || !selectedFiles.person || !selectedFiles.garment) return;
  stopObservation();
  const interaction = ++interactionToken;
  const historyEpochAtSubmit = historyEpoch;
  submissionInFlight = true;
  const key = retryKey || crypto.randomUUID();
  retryKey = key;
  syncSubmit();
  try {
    // Reset/navigation only cancels observation: an accepted POST still gets a local record.
    const data = await requestJson('/api/tryon/tasks', {
      method: 'POST', headers: { 'X-Garment-Category': document.querySelector('#category').value, 'Idempotency-Key': key }, body: new FormData(form)
    }, undefined, 15000);
    if (retryKey === key) retryKey = null;
    if (historyEpochAtSubmit === historyEpoch) saveHistory(data);
    if (interaction === interactionToken) {
      selectedTaskId = data.taskId;
      renderHistory();
      void observeTask(data.taskId, data);
    }
  } catch (error) {
    showToast(error.name === 'TimeoutError' ? '提交响应超时。保留当前图片重试会查询同一任务。' : error.message, 'error');
  } finally {
    submissionInFlight = false;
    syncSubmit();
  }
});

function render(data, snapshotMessage = '') {
  currentTaskData = data;
  currentTaskSource = snapshotMessage ? 'local-history-snapshot' : 'live-task';
  setTaskToolsEnabled(Boolean(data?.taskId));
  const snapshotNotice = document.querySelector('#snapshotNotice');
  snapshotNotice.hidden = !snapshotMessage;
  snapshotNotice.textContent = snapshotMessage;
  document.querySelector('.status-title').textContent = snapshotMessage ? '上次记录的任务状态' : '换装任务';
  for (const [id, value] of [['createdMeta', data.createdAt], ['updatedMeta', data.updatedAt]]) {
    const node = document.querySelector(`#${id}`);
    node.textContent = formatDate(value);
    if (value) node.dateTime = value;
    else node.removeAttribute('datetime');
  }
  empty.style.display = 'none';
  view.classList.add('visible');
  document.querySelector('#taskId').textContent = data.taskId;
  const badge = document.querySelector('#badge');
  badge.textContent = data.status;
  badge.className = `badge ${data.status.toLowerCase()}`;
  document.querySelector('#modeMeta').textContent = data.mode || 'mock';
  document.querySelector('#personMeta').textContent = data.inputs?.person ? `${data.inputs.person.width} × ${data.inputs.person.height}` : '-';
  document.querySelector('#garmentMeta').textContent = data.inputs?.garment ? `${data.inputs.garment.width} × ${data.inputs.garment.height}` : '-';
  document.querySelector('#durationMeta').textContent = data.durationMs == null ? (snapshotMessage || terminalStatuses.has(data.status) ? '未记录' : '处理中') : duration(data.durationMs);
  renderTimeline(data);
  const order = { QUEUED: 0, RUNNING: 1, SUCCEEDED: 2, FAILED: 2 };
  document.querySelectorAll('.step').forEach(step => {
    const stepStatus = step.dataset.step;
    const failedStep = data.status === 'FAILED' && stepStatus === 'RUNNING';
    step.classList.toggle('active', stepStatus === data.status || failedStep);
    step.classList.toggle('failed', failedStep);
    step.classList.toggle('done', data.status === 'FAILED' ? stepStatus === 'QUEUED' : order[stepStatus] < order[data.status] || (data.status === 'SUCCEEDED' && stepStatus === 'SUCCEEDED'));
  });
  const figure = document.querySelector('#outputFigure');
  const image = document.querySelector('#outputImage');
  const download = document.querySelector('#downloadResult');
  const showImage = data.mode === 'catvton' && data.status === 'SUCCEEDED' && !snapshotMessage;
  figure.hidden = !showImage;
  if (showImage) {
    const path = `/api/tryon/tasks/${encodeURIComponent(data.taskId)}/result`;
    if (image.getAttribute('src') !== path) image.src = path;
    download.href = path;
    download.download = `tryon-${data.taskId}.png`;
  } else {
    image.removeAttribute('src');
    download.removeAttribute('href');
  }
  const note = document.querySelector('#resultNote');
  note.className = 'result-note';
  if (data.status === 'SUCCEEDED') {
    note.classList.add('success');
    note.textContent = data.mode === 'mock' ? '接口演练已完成。' : (snapshotMessage ? '正在核对已保存的结果。' : '试穿图已保存。');
  } else if (data.status === 'FAILED') {
    note.classList.add('error');
    const messages = {
      GARMENT_REGION_NOT_FOUND: '人物照片中没有找到所选部位的衣物。请检查试穿部位，或换一张正面照。',
      INFERENCE_TIMEOUT: '推理超时。可以重新提交这组图片。',
      SERVER_RESTARTED: '任务因服务重启中断，可以重新提交。',
      INFERENCE_BUSY: '模型正忙，请稍后重试。',
      MODEL_MEMORY_LIMIT: '模型显存不足。请降低服务的输出分辨率后重试。'
    };
    note.textContent = messages[data.error] || `任务失败（${data.error || '未知错误'}）`;
  } else if (snapshotMessage) {
    note.textContent = '上次记录尚未完成；当前状态未知。';
  } else {
    note.textContent = data.status === 'RUNNING' ? '推理服务正在处理输入…' : '任务已排队，等待 worker 处理…';
  }
}

reset.addEventListener('click', () => {
  interactionToken += 1;
  stopObservation();
  selectedTaskId = null;
  currentTaskData = null;
  setTaskToolsEnabled(false);
  HTMLFormElement.prototype.reset.call(form);
  clearFile('person');
  clearFile('garment');
  syncSubmit();
  empty.style.display = 'grid';
  view.classList.remove('visible');
  renderHistory();
});

copySummary.addEventListener('click', async () => {
  if (!currentTaskData) return;
  try {
    await writeClipboard(JSON.stringify(summaryPayload(currentTaskData, currentTaskSource), null, 2));
    showToast('任务摘要已复制到剪贴板');
  } catch (error) {
    showToast(error.message || '复制失败，请重试', 'error');
  }
});

downloadSummary.addEventListener('click', () => {
  if (!currentTaskData) return;
  downloadTaskSummary(currentTaskData);
  showToast('已请求下载摘要，请在浏览器下载记录中查看');
});

document.querySelector('#clearHistory').addEventListener('click', () => {
  // Stop background queries from immediately repopulating a cleared history.
  interactionToken += 1;
  historyEpoch += 1;
  stopObservation();
  store.clear();
  selectedTaskId = null;
  renderHistory();
  showToast('本次页面的历史已清空');
});

function setTheme(theme) {
  theme = theme === 'dark' ? 'dark' : 'light';
  document.documentElement.dataset.theme = theme;
  store.set('tryon.theme', theme);
  themeToggle.textContent = theme === 'dark' ? '☼' : '◐';
  themeToggle.setAttribute('aria-label', theme === 'dark' ? '切换到浅色主题' : '切换到深色主题');
}
setTheme(store.get('tryon.theme') || 'light');
themeToggle.addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));

let checkingService = false;
async function checkService() {
  if (checkingService || document.hidden) return;
  checkingService = true;
  refreshService.disabled = true;
  refreshService.setAttribute('aria-busy', 'true');
  try {
    const [readyResult, healthResult] = await Promise.allSettled([
      requestJson('/health/ready'),
      requestJson('/health')
    ]);
    if (readyResult.status === 'fulfilled') {
      const data = readyResult.value;
      const mode = healthResult.status === 'fulfilled' ? healthResult.value.mode : '';
      document.querySelector('#serviceText').textContent = data.status !== 'READY' ? '服务未就绪' : (mode === 'catvton' ? 'CatVTON 已就绪' : '接口演练服务');
      document.querySelector('#serviceDot').classList.toggle('off', data.status !== 'READY');
    } else {
      document.querySelector('#serviceText').textContent = '暂时无法确认服务就绪';
      document.querySelector('#serviceDot').classList.add('off');
    }
    renderTelemetry(healthResult.status === 'fulfilled' ? healthResult.value : null);
  } finally {
    checkingService = false;
    refreshService.disabled = false;
    refreshService.setAttribute('aria-busy', 'false');
  }
}
refreshService.addEventListener('click', checkService);
document.addEventListener('visibilitychange', () => { if (!document.hidden) checkService(); });
renderHistory();
checkService();
setInterval(checkService, 15000);
