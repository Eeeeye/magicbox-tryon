// Real acceptance: start scripts/run-local.sh first. Requires Playwright and a Chromium browser.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');

async function main() {
  const base = process.env.TRYON_URL || 'http://127.0.0.1:8780';
  const worker = process.env.WORKER_URL || 'http://127.0.0.1:8781';
  const category = process.env.TRYON_TEST_CATEGORY || 'upper';
  assert.ok(['upper', 'lower', 'dress'].includes(category));
  const output = path.resolve(process.env.EVIDENCE_DIR || 'evidence');
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_BIN || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 1360, height: 940 }, acceptDownloads: true });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base);
    const health = await (await page.request.get(base + '/health')).json();
    assert.equal(health.mode, 'catvton');
    const model = await (await page.request.get(worker + '/ready')).json();
    assert.equal(model.model, 'CatVTON-Mix');
    await page.locator('#category').selectOption(category);
    await page.getByRole('button', { name: '使用公开样例' }).click();
    if (process.env.TRYON_TEST_PERSON) {
      await page.locator('input[name="person"]').setInputFiles(process.env.TRYON_TEST_PERSON);
    }
    await page.waitForFunction(() => [...document.querySelectorAll('.preview')].every(i => i.naturalWidth > 0));
    const [accepted] = await Promise.all([
      page.waitForResponse(r => r.url().endsWith('/api/tryon/tasks') && r.request().method() === 'POST'),
      page.getByRole('button', { name: '开始试穿' }).click()
    ]);
    assert.equal(accepted.status(), 202);
    const created = await accepted.json();
    console.log('Accepted real task', created.taskId);
    await page.waitForFunction(() => ['SUCCEEDED', 'FAILED'].includes(document.querySelector('#badge').textContent), null,
                               { timeout: 900000, polling: 750 });
    const final = await (await page.request.get(base + '/api/tryon/tasks/' + created.taskId)).json();
    assert.equal(final.status, 'SUCCEEDED', JSON.stringify(final));
    assert.equal(final.mode, 'catvton');
    assert.equal(final.category, category);
    assert.ok(final.durationMs > 0);
    await page.locator('#outputImage').waitFor();
    await page.waitForFunction(() => document.querySelector('#outputImage').naturalWidth > 0);
    const dimensions = await page.locator('#outputImage').evaluate(i => [i.naturalWidth, i.naturalHeight]);
    assert.deepEqual(dimensions, [model.width, model.height]);
    await fs.mkdir(output, { recursive: true });
    const [download] = await Promise.all([page.waitForEvent('download'), page.locator('#downloadResult').click()]);
    const result = path.join(output, 'tryon-result.png');
    await download.saveAs(result);
    const image = await fs.readFile(result);
    assert.equal(image.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
    const served = await page.request.get(base + final.result);
    assert.equal(served.headers()['content-type'], 'image/png');
    assert.deepEqual(await served.body(), image);
    const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
    for (const role of ['person', 'garment']) {
      const fileName = role === 'person'
        ? (category === 'lower' ? 'person-lower.png' : 'person.jpg')
        : (category === 'upper' ? 'garment.jpg' : category + '.jpg');
      const inputPath = role === 'person' && process.env.TRYON_TEST_PERSON ? process.env.TRYON_TEST_PERSON : path.join('samples', fileName);
      const input = await fs.readFile(inputPath);
      assert.equal(hash(input), final.inputs[role].sha256);
      assert.notEqual(hash(image), hash(input));
    }
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: path.join(output, 'product.png') });
    await page.reload();
    await page.locator('.history-item').filter({ hasText: created.taskId.slice(0, 8) }).first().click();
    await page.waitForFunction(() => !document.querySelector('#outputFigure').hidden && document.querySelector('#outputImage').naturalWidth > 0);
    assert.deepEqual(errors, []);
    const report = { verifiedAt: new Date().toISOString(), inputs: 'pinned public demo samples in inference/assets.lock.json',
      model, task: final, downloaded: { filename: download.suggestedFilename(), bytes: image.length, sha256: hash(image), dimensions },
      browser: { engine: 'Chromium', version: browser.version(), checks: ['upload two sample files', 'submit',
        'poll to SUCCEEDED', 'render result', 'download PNG byte-equal to API', 'reload and reopen history'] } };
    await fs.writeFile(path.join(output, 'acceptance.json'), JSON.stringify(report, null, 2) + '\n');
    console.log('Verified', JSON.stringify(report.downloaded), 'model duration (ms)', final.durationMs);
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
