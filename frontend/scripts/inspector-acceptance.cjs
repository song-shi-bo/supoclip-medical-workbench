/**
 * Inspector Phase 1 — 真实浏览器验收（独立 Playwright 实例，避免与其他代理共享的 MCP 浏览器互相干扰）。
 *
 * 前置：工作台已用 /domain/video-brain/assets/source.mp4（1920×1080 / 45s）跑过一次真实转写+自动剪辑，
 *       台账项目 ACC_PROJECT 里有成片（默认 timeline_790a6b0c9d87，2 条 clip）。
 *
 * 用法（frontend/ 目录）：
 *   ACC_ONLY=tabs node scripts/inspector-acceptance.cjs     # 只跑某一步
 *   node scripts/inspector-acceptance.cjs                   # 全跑
 */
const { chromium } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const BASE = 'http://127.0.0.1:3107';
const PROJECT = process.env.ACC_PROJECT || 'timeline_790a6b0c9d87';
const CLIP = process.env.ACC_CLIP || 'clip-fa7c20-01';
const OUT = '/domain/supoclip-medical-workbench/docs/screenshots/inspector-phase1';
const ONLY = process.env.ACC_ONLY || '';
const BGM_FILE = process.env.ACC_BGM || '/tmp/acc/bgm-test.mp3';

fs.mkdirSync(OUT, { recursive: true });
const results = [];
const evidence = {};
const log = (...a) => console.log(...a);
function rec(id, ok, detail) {
  results.push({ id, ok: !!ok, detail: String(detail).slice(0, 600) });
  log(`${ok ? 'PASS' : 'FAIL'} [${id}] ${String(detail).slice(0, 280)}`);
}
const step = (name) => !ONLY || ONLY === name;

function findChromium() {
  for (const candidate of [
    '/opt/ms-playwright/chromium-1246/chrome-linux64/chrome',
    '/opt/ms-playwright/chromium-1246/chrome-linux/chrome',
    '/opt/ms-playwright/chromium_headless_shell-1246/chrome-headless-shell-linux64/chrome-headless-shell',
  ]) { if (fs.existsSync(candidate)) return candidate; }
  return undefined;
}

async function boot(browser, viewport = { width: 1600, height: 900 }) {
  const ctx = await browser.newContext({ viewport });
  const page = await ctx.newPage();
  const net = [];
  page.on('request', (r) => {
    const u = r.url();
    if (/\/studio\/(clips|projects|auto-edit)/.test(u) && r.method() !== 'GET') {
      let body = ''; try { body = r.postData() || ''; } catch { body = ''; }
      net.push({ method: r.method(), url: u.replace(BASE, '').replace('/video-api', ''), body: body.slice(0, 900) });
    }
  });
  await page.goto(`${BASE}/studio`, { waitUntil: 'domcontentloaded' });
  await page.evaluate((pid) => localStorage.setItem('supoclip-studio-session', JSON.stringify({ projectId: pid, taskId: null })), PROJECT);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.asset-card.rendered-card', { timeout: 45000 });
  return { ctx, page, net };
}

const shot = async (page, name) => { await page.screenshot({ path: path.join(OUT, `${name}.png`) }); };

async function selectCard(page, index = 0) {
  await page.locator('.asset-card.rendered-card').nth(index).click();
  await page.waitForTimeout(1600);
}
const TAB_ID = { '转写': 'transcript', '字幕': 'subtitle', '封面与画面': 'cover', '封面': 'cover', '音频': 'audio', '效果设置': 'effects', '效果': 'effects' };
async function openTab(page, label) {
  const id = TAB_ID[label];
  await page.locator(`[data-testid="inspector-tab-${id}"]`).click();
  await page.waitForTimeout(1500);
}
const paneText = (page) => page.locator('[data-testid="inspector-pane"]').innerText();
async function apiGet(page, p) {
  return page.evaluate(async (url) => {
    const r = await fetch(`/video-api${url}`, { cache: 'no-store' });
    let body = null; try { body = await r.json(); } catch { body = null; }
    return { status: r.status, body };
  }, p);
}

(async () => {
  const browser = await chromium.launch({ executablePath: findChromium() });
  const { page, net } = await boot(browser);
  fs.writeFileSync('/tmp/acc/out.txt', OUT);

  const reloadAndBind = async () => {
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.asset-card.rendered-card', { timeout: 45000 });
    await selectCard(page, 0);
  };

  // ---------------------------------------------------------------- 0. 绑定
  const cardCount = await page.locator('.asset-card.rendered-card').count();
  rec('accept.0.ledger-cards', cardCount >= 1, `${cardCount} 张成片卡片（台账事实源）`);
  await selectCard(page, 0);
  await shot(page, '03-clip1-selected');

  // ---------------------------------------------------------------- 1. 五标签 + 绑定切换
  if (step('tabs')) {
    const tabs = (await page.locator('[data-testid="inspector-pane"] .inspector-tabs button').allInnerTexts()).map((t) => t.trim());
    const titles = await page.locator('[data-testid="inspector-pane"] .inspector-tabs button').evaluateAll((els) => els.map((e) => e.getAttribute('title')));
    const ok1 = JSON.stringify(tabs) === JSON.stringify(['转写', '字幕', '封面与画面', '音频', '效果设置']);
    const ok2 = JSON.stringify(tabs) === JSON.stringify(['转写', '字幕', '封面', '音频', '效果']);
    rec('accept.1.tabs', ok1 || ok2, `tabs=${JSON.stringify(tabs)} titles=${JSON.stringify(titles)}`);
    const seen = {};
    for (const [key, label] of [['transcript', '转写'], ['subtitle', '字幕'], ['cover', '封面与画面'], ['audio', '音频'], ['effects', '效果设置']]) {
      await openTab(page, label);
      seen[key] = (await paneText(page)).slice(0, 260).replace(/\n/g, ' | ');
      await shot(page, `04-tab-${key}`);
    }
    evidence.tabs = seen;
    rec('accept.1.tabs-real', Object.values(seen).every((t) => t.length > 40), '五个标签都有真实内容（非静态标签）');

    await openTab(page, '封面与画面');
    const asset1 = await page.locator('[data-testid="inspector-cover-asset"]').innerText();
    await selectCard(page, 1);
    await openTab(page, '封面与画面');
    const asset2 = await page.locator('[data-testid="inspector-cover-asset"]').innerText();
    rec('accept.1.rebind', asset1 !== asset2, `clip1 ${asset1.trim()} → clip2 ${asset2.trim()}`);
    evidence.cover_binding = { asset1: asset1.trim(), asset2: asset2.trim() };
    await selectCard(page, 0);
  }

  // ---------------------------------------------------------------- 2. 转写
  if (step('transcript')) {
    await reloadAndBind();
    await openTab(page, '转写');
    await page.waitForSelector('[data-testid^="inspector-segment-"]', { timeout: 25000 });
    const segCount = await page.locator('[data-testid^="inspector-segment-"]').count();
    const notice = await page.locator('[data-testid="inspector-asr-notice"]').innerText();
    rec('accept.2.rows', segCount > 0 && notice.includes('原始 ASR 不会被覆盖'), `${segCount} 段；notice=${notice.slice(0, 40)}`);

    const variantText = async (id) => {
      await page.locator(`[data-testid="inspector-variant-${id}"]`).click();
      await page.waitForTimeout(900);
      return (await page.locator('[data-testid^="inspector-segment-"]').first().innerText()).replace(/\n/g, ' ');
    };
    const raw = await variantText('raw');
    const corrected = await variantText('corrected');
    const effective = await variantText('effective');
    evidence.three_state = { raw, corrected, effective };
    rec('accept.2.three-state', raw !== corrected && corrected === effective,
      `raw=${raw.slice(0, 44)} | corrected=${corrected.slice(0, 44)} | eff=${effective.slice(0, 44)}`);

    const search = page.locator('[data-testid="inspector-transcript-tab"] input[aria-label="搜索转写内容"]');
    await search.fill('血压');
    await page.waitForTimeout(900);
    const hits = await page.locator('[data-testid="inspector-hit-count"]').innerText();
    const marks = await page.locator('[data-testid="inspector-pane"] mark').count();
    rec('accept.2.search', /[1-9]\d*\/\d+/.test(hits) && marks > 0, `hit-count=${hits.trim()} marks=${marks}`);
    evidence.search = hits.trim();
    await shot(page, '05-transcript-search');
    await search.fill('');
    await page.waitForTimeout(500);

    const timeBefore = await page.locator('[aria-label="播放控制栏"]').innerText();
    await page.locator('[data-testid^="inspector-segment-"]').nth(2).click();
    await page.waitForTimeout(1400);
    const timeAfter = await page.locator('[aria-label="播放控制栏"]').innerText();
    rec('accept.2.seek', timeBefore !== timeAfter, `${timeBefore.replace(/\n/g, ' ')} → ${timeAfter.replace(/\n/g, ' ')}`);

    const target = page.locator('[data-testid^="inspector-segment-"]').nth(2);
    const segId = (await target.getAttribute('data-testid')).replace('inspector-segment-', '');
    const original = await target.locator('.inspector-segment-text').innerText();
    await target.locator(`button[aria-label="编辑分段 ${segId}"]`).click();
    const edited = original + '（验收修订）';
    await target.locator(`textarea[aria-label="分段文本 ${segId}"]`).fill(edited);
    await target.getByRole('button', { name: '保存修订' }).click();
    await page.waitForTimeout(3000);
    const revLine = await page.locator('[data-testid="inspector-transcript-revision"]').innerText();
    rec('accept.2.revision', /修订 v[1-9]/.test(revLine), `revision line = ${revLine.trim()}`);
    evidence.revision = revLine.trim();
    await page.locator('[data-testid="inspector-toggle-history"]').click();
    await page.waitForTimeout(800);
    const history = await page.locator('[data-testid="inspector-transcript-history"]').innerText();
    rec('accept.2.history', history.includes('v1'), `history=${history.replace(/\n/g, ' | ').slice(0, 120)}`);
    await shot(page, '06-transcript-revision');

    await reloadAndBind();
    await openTab(page, '转写');
    await page.waitForSelector('[data-testid^="inspector-segment-"]', { timeout: 25000 });
    const afterReload = await page.locator(`[data-testid="inspector-segment-${segId}"] .inspector-segment-text`).innerText();
    const revAfterReload = await page.locator('[data-testid="inspector-transcript-revision"]').innerText();
    rec('accept.2.persist', afterReload.includes('验收修订') && /修订 v[1-9]/.test(revAfterReload), `${afterReload.slice(0, 34)} | ${revAfterReload.trim()}`);
    const rawAfter = await variantText('raw');
    rec('accept.2.raw-untouched', rawAfter === raw && !rawAfter.includes('验收修订'), `raw 仍是原始 ASR：${rawAfter.slice(0, 44)}`);
    await variantText('effective');
    await shot(page, '07-transcript-after-reload');
  }

  // ---------------------------------------------------------------- 3. 字幕
  if (step('subtitle')) {
    await reloadAndBind();
    await openTab(page, '字幕');
    await page.waitForSelector('[data-testid^="inspector-cue-"]', { timeout: 25000 });
    const cueCount = await page.locator('[data-testid^="inspector-cue-"]').count();
    const overlayBefore = await page.locator('[data-testid="inspector-subtitle-cue"]').innerText().catch(() => '');
    const styleBefore = await page.locator('[data-testid="inspector-subtitle-cue"]').getAttribute('style');
    rec('accept.3.cues', cueCount > 0 && overlayBefore.length > 0, `cues=${cueCount} overlay="${overlayBefore.slice(0, 24)}"`);

    const cid = (await page.locator('[data-testid^="inspector-cue-"]').first().getAttribute('data-testid')).replace('inspector-cue-', '');
    const cueText = page.locator(`textarea[aria-label="字幕文本 ${cid}"]`);
    await cueText.fill('字幕实时预览验收');
    await page.waitForTimeout(1000);
    const overlayAfter = await page.locator('[data-testid="inspector-subtitle-cue"]').innerText();
    rec('accept.3.live-overlay', overlayAfter.includes('字幕实时预览验收'), `overlay="${overlayAfter.slice(0, 30)}"（改文字后立即更新）`);
    const trackText = await page.locator('.caption-track').innerText();
    rec('accept.3.timeline-track', trackText.includes('字幕实时预览验收'), `时间线字幕轨已同步：${trackText.replace(/\n/g, ' ').slice(0, 60)}`);

    const sizeInput = page.locator('[data-testid="inspector-subtitle-tab"] input[aria-label="字幕字号"]');
    await sizeInput.fill('80');
    await sizeInput.dispatchEvent('change');
    await page.waitForTimeout(1000);
    const styleAfter = await page.locator('[data-testid="inspector-subtitle-cue"]').getAttribute('style');
    const fs2 = /font-size:\s*([\d.]+)px/.exec(styleAfter || '');
    const fs1 = /font-size:\s*([\d.]+)px/.exec(styleBefore || '');
    rec('accept.3.style-live', !!fs2 && Number(fs2[1]) > Number(fs1 ? fs1[1] : 0), `font-size ${fs1 ? fs1[1] : '?'}px → ${fs2 ? fs2[1] : '?'}px`);
    evidence.overlay_style = { before: styleBefore, after: styleAfter };
    await shot(page, '08-subtitle-live');

    await page.locator('[data-testid="inspector-subtitle-save"]').click();
    await page.waitForTimeout(5000);
    const latest = await page.locator('[data-testid="inspector-subtitle-latest"]').innerText();
    rec('accept.3.saved-rev', /rev-[1-9]/.test(latest), `latest=${latest.trim()}`);
    await reloadAndBind();
    await openTab(page, '字幕');
    await page.waitForSelector('[data-testid^="inspector-cue-"]', { timeout: 25000 });
    const cueAfterReload = await page.locator(`textarea[aria-label="字幕文本 ${cid}"]`).inputValue();
    const latestAfter = await page.locator('[data-testid="inspector-subtitle-latest"]').innerText();
    const sizeAfterReload = await page.locator('[data-testid="inspector-subtitle-tab"] input[aria-label="字幕字号"]').inputValue();
    rec('accept.3.persist', cueAfterReload.includes('字幕实时预览验收') && /rev-[1-9]/.test(latestAfter) && sizeAfterReload === '80',
      `text="${cueAfterReload.slice(0, 20)}" size=${sizeAfterReload} ${latestAfter.trim()}`);
    await shot(page, '09-subtitle-after-reload');
  }

  // ---------------------------------------------------------------- 4. 封面
  if (step('cover')) {
    await reloadAndBind();
    await openTab(page, '封面与画面');
    const cardsBefore = await page.locator('.asset-card.rendered-card').count();
    const revBefore = await page.locator('[data-testid="inspector-cover-revision"]').innerText();
    const assetBefore = await page.locator('[data-testid="inspector-cover-asset"]').innerText();
    const tasksBefore = await apiGet(page, `/studio/projects/${PROJECT}/tasks`);
    const beforeIds = new Set((tasksBefore.body?.tasks || []).map((t) => t.task_id));
    await page.locator('[data-testid="inspector-cover-regenerate"]').click();
    let newTask = null;
    for (let i = 0; i < 40; i += 1) {
      const tasks = await apiGet(page, `/studio/projects/${PROJECT}/tasks`);
      const fresh = (tasks.body?.tasks || []).filter((t) => !beforeIds.has(t.task_id));
      if (fresh.length) { newTask = fresh[fresh.length - 1]; break; }
      await page.waitForTimeout(1500);
    }
    for (let i = 0; i < 40 && newTask; i += 1) {
      if (['completed', 'failed'].includes(newTask.status)) break;
      await page.waitForTimeout(3000);
      const t = await apiGet(page, `/studio/projects/${PROJECT}/tasks`);
      newTask = (t.body?.tasks || []).find((x) => x.task_id === newTask.task_id) || newTask;
    }
    rec('accept.4.task-type', (newTask || {}).task_type === 'REGENERATE_COVER', `新任务 task_type=${(newTask || {}).task_type} status=${(newTask || {}).status} id=${(newTask || {}).task_id}`);
    const allTypes = (await apiGet(page, `/studio/projects/${PROJECT}/tasks`)).body?.tasks?.map((t) => t.task_type) || [];
    rec('accept.4.no-autoedit', !allTypes.includes('AUTO_EDIT'), `项目任务类型集合=${JSON.stringify([...new Set(allTypes)])}`);
    await page.waitForTimeout(3000);
    const cardsAfter = await page.locator('.asset-card.rendered-card').count();
    await openTab(page, '封面与画面');
    const revAfter = await page.locator('[data-testid="inspector-cover-revision"]').innerText();
    const assetAfter = await page.locator('[data-testid="inspector-cover-asset"]').innerText();
    rec('accept.4.card-count', cardsAfter === cardsBefore, `卡片 ${cardsBefore} → ${cardsAfter}（原地刷新，不新增）`);
    rec('accept.4.revision', revAfter !== revBefore && assetAfter !== assetBefore, `${revBefore.trim()}/${assetBefore.trim()} → ${revAfter.trim()}/${assetAfter.trim()}`);
    evidence.cover = { revBefore: revBefore.trim(), revAfter: revAfter.trim(), assetBefore: assetBefore.trim(), assetAfter: assetAfter.trim(), task: newTask && { task_id: newTask.task_id, task_type: newTask.task_type, status: newTask.status } };
    await shot(page, '10-cover-regenerated');
  }

  // ---------------------------------------------------------------- 5. 画面（变换）
  if (step('picture')) {
    await reloadAndBind();
    await openTab(page, '封面与画面');
    await page.locator('[aria-label="裁切/重构图"]').click();
    await page.waitForTimeout(1500);
    const stage = page.locator('.transform-stage').first();
    const box = await stage.boundingBox();
    rec('accept.5.crop-mode', !!box, `进入裁切模式，stage box=${box ? `${Math.round(box.width)}x${Math.round(box.height)}` : 'none'}`);
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2 + 60, box.y + box.height / 2 + 30, { steps: 12 });
    await page.mouse.up();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.wheel(0, -240);
    await page.waitForTimeout(1200);
    await shot(page, '11-transform-draft');
    const netBeforeApply = net.length;
    await page.locator('.transform-panel button[aria-label="应用"]').click();
    await page.waitForTimeout(3000);
    const applied = net.slice(netBeforeApply).filter((n) => n.method === 'PUT' && /\/config$/.test(n.url));
    rec('accept.5.apply-put', applied.length > 0, `PUT ${applied.map((a) => a.url).join(',')} body=${(applied[0] || {}).body?.slice(0, 170)}`);
    evidence.transform_apply = applied[0] || null;
    await reloadAndBind();
    await openTab(page, '封面与画面');
    const cfg = await apiGet(page, `/studio/clips/${PROJECT}/${CLIP}/config`);
    const savedTransform = cfg.body?.config?.transform || null;
    rec('accept.5.persist', !!savedTransform,
      `持久化 transform=${JSON.stringify(savedTransform)?.slice(0, 170)}`);
    await page.locator('[aria-label="裁切/重构图"]').click();
    await page.waitForTimeout(1200);
    const netBeforeCancel = net.length;
    const stage2 = page.locator('.transform-stage').first();
    const box2 = await stage2.boundingBox();
    await page.mouse.move(box2.x + box2.width / 2, box2.y + box2.height / 2);
    await page.mouse.wheel(0, -180);
    await page.waitForTimeout(900);
    await page.locator('.transform-panel button[aria-label="取消"]').click();
    await page.waitForTimeout(2500);
    const writes = net.slice(netBeforeCancel).filter((n) => n.method !== 'GET' && /\/config$/.test(n.url));
    const cfg2 = await apiGet(page, `/studio/clips/${PROJECT}/${CLIP}/config`);
    rec('accept.5.cancel-no-write', writes.length === 0 && JSON.stringify(cfg2.body?.config?.transform) === JSON.stringify(savedTransform),
      `取消后写请求=${writes.length}，transform 不变`);
    await shot(page, '12-transform-persisted');
  }

  // ---------------------------------------------------------------- 6. 音频
  if (step('audio')) {
    await reloadAndBind();
    await openTab(page, '音频');
    const voiceRow = await page.locator('[data-testid="inspector-waveform-voice-row"]').count();
    const bgmRowBefore = await page.locator('[data-testid="inspector-waveform-bgm-row"]').count();
    rec('accept.6.no-empty-category', voiceRow === 1 && bgmRowBefore === 0, `原声轨=${voiceRow} BGM 轨=${bgmRowBefore}（未上传时只有原声，不出现空素材分类）`);
    await shot(page, '13-audio-no-bgm');

    await page.locator('input[aria-label="BGM 文件"]').setInputFiles(BGM_FILE);
    await page.waitForTimeout(1200);
    await page.locator('[data-testid="inspector-bgm-upload"]').click();
    let uploaded = null;
    for (let i = 0; i < 40; i += 1) {
      const cfg = await apiGet(page, `/studio/clips/${PROJECT}/${CLIP}/config`);
      const tracks = cfg.body?.config?.audio_tracks || [];
      if (tracks.length) { uploaded = tracks[0]; break; }
      await page.waitForTimeout(1500);
    }
    rec('accept.6.bgm-upload', !!uploaded, `audio_tracks=${JSON.stringify(uploaded)?.slice(0, 230)}`);
    await page.waitForTimeout(2000);
    const bgmRowAfter = await page.locator('[data-testid="inspector-waveform-bgm-row"]').count();
    const canvasPixels = await page.evaluate(() => {
      const canvas = document.querySelector('[data-testid="inspector-waveform-bgm-row"] canvas, [data-testid="inspector-audio-waveform"] canvas');
      if (!canvas) return null;
      const ctx = canvas.getContext('2d');
      const data = ctx.getImageData(0, 0, Math.min(200, canvas.width), Math.min(60, canvas.height)).data;
      let painted = 0;
      for (let i = 3; i < data.length; i += 4) if (data[i] > 0) painted += 1;
      return { width: canvas.width, height: canvas.height, painted };
    });
    rec('accept.6.waveform', bgmRowAfter === 1 && !!canvasPixels && canvasPixels.painted > 0,
      `BGM 轨=${bgmRowAfter} canvas=${JSON.stringify(canvasPixels)}`);
    await shot(page, '14-audio-bgm');

    const volBefore = net.length;
    const volInput = page.locator('input[aria-label="BGM 音量"]');
    await volInput.fill('0.4'); await volInput.dispatchEvent('change');
    const fadeIn = page.locator('input[aria-label="BGM 淡入秒数"]'); await fadeIn.fill('0.5'); await fadeIn.dispatchEvent('change');
    const fadeOut = page.locator('input[aria-label="BGM 淡出秒数"]'); await fadeOut.fill('0.7'); await fadeOut.dispatchEvent('change');
    await page.waitForTimeout(3000);
    const volWrites = net.slice(volBefore).filter((n) => n.method === 'PUT' && /\/config$/.test(n.url));
    const cfg3 = await apiGet(page, `/studio/clips/${PROJECT}/${CLIP}/config`);
    const track3 = (cfg3.body?.config?.audio_tracks || [])[0] || {};
    rec('accept.6.bgm-settings', volWrites.length > 0 && Number(track3.volume) === 0.4,
      `PUT=${volWrites.length} volume=${track3.volume} fade_in=${track3.fade_in_s} fade_out=${track3.fade_out_s}`);

    const voice = page.locator('input[aria-label="原声音量"]');
    await voice.fill('0.6'); await voice.dispatchEvent('change');
    await page.waitForTimeout(2500);
    const cfg4 = await apiGet(page, `/studio/clips/${PROJECT}/${CLIP}/config`);
    rec('accept.6.voice', Number(cfg4.body?.config?.voice_volume) === 0.6, `voice_volume=${cfg4.body?.config?.voice_volume} muted=${cfg4.body?.config?.muted}`);

    await page.locator('[data-testid="inspector-bgm-delete"]').click();
    await page.waitForTimeout(3000);
    const cfg5 = await apiGet(page, `/studio/clips/${PROJECT}/${CLIP}/config`);
    const bgmRowGone = await page.locator('[data-testid="inspector-waveform-bgm-row"]').count();
    rec('accept.6.bgm-delete', (cfg5.body?.config?.audio_tracks || []).length === 0 && bgmRowGone === 0, `删除后 tracks=${(cfg5.body?.config?.audio_tracks || []).length} BGM 轨=${bgmRowGone}`);
    await page.locator('input[aria-label="原声音量"]').fill('1');
    await page.locator('input[aria-label="原声音量"]').dispatchEvent('change');
    await page.waitForTimeout(2000);
    await shot(page, '15-audio-after-delete');
  }

  // ---------------------------------------------------------------- 7. 效果
  if (step('effects')) {
    await reloadAndBind();
    await openTab(page, '效果设置');
    const netBefore = net.length;
    await page.locator('[aria-label="医生身份标识开关"]').click();
    await page.waitForTimeout(900);
    await page.locator('[aria-label="标识位置"]').selectOption('right');
    await page.locator('[aria-label="片尾卡开关"]').click();
    await page.locator('[aria-label="片尾文案"]').fill('关注我，了解更多');
    await page.locator('[aria-label="滤镜"]').selectOption('warm');
    const strength = page.locator('[aria-label="滤镜强度"]'); await strength.fill('0.6'); await strength.dispatchEvent('change');
    await page.locator('[aria-label="导出分辨率"]').selectOption('1080x1080');
    await page.waitForTimeout(3500);
    const writes = net.slice(netBefore).filter((n) => n.method === 'PUT' && /\/config$/.test(n.url));
    const cfg = await apiGet(page, `/studio/clips/${PROJECT}/${CLIP}/config`);
    const fx = cfg.body?.config?.effects || {};
    rec('accept.7.saved', writes.length > 0 && fx.sticker?.enabled === true && fx.sticker?.position === 'right' && fx.end_card?.enabled === true && fx.filter?.name === 'warm' && fx.export_resolution === '1080x1080',
      `PUT=${writes.length} effects=${JSON.stringify(fx)}`);
    evidence.effects_writes = writes.map((w) => ({ url: w.url, body: w.body.slice(0, 220) }));
    const summary = await apiGet(page, `/studio/clips/${PROJECT}/${CLIP}/render-config`);
    const byKey = Object.fromEntries((summary.body?.effective || []).map((e) => [e.key, e]));
    rec('accept.7.render-config', byKey['effects.filter.name']?.value === 'warm' && byKey['effects.filter.name']?.source === 'user' && byKey['effects.export_resolution']?.value === '1080x1080',
      `filter=${JSON.stringify(byKey['effects.filter.name'])} resolution=${JSON.stringify(byKey['effects.export_resolution'])}`);
    const paneTxt = await paneText(page);
    rec('accept.7.phase2-labels', paneTxt.includes('Phase 2') && (await page.locator('[data-testid="inspector-resolution-phase2"]').count()) > 0,
      `Phase 2 标注可见；摘要含「用户修改」=${paneTxt.includes('用户修改')}`);
    await shot(page, '16-effects-saved');
    evidence.render_config = { filter: byKey['effects.filter.name'], resolution: byKey['effects.export_resolution'], export: summary.body?.export };
  }

  // ---------------------------------------------------------------- 8. 响应式
  if (step('responsive')) {
    for (const [name, vp] of [['1366x768', { width: 1366, height: 768 }], ['1024x768', { width: 1024, height: 768 }]]) {
      await page.setViewportSize(vp);
      await page.waitForTimeout(2000);
      const geo = await page.evaluate(() => {
        const box = (sel) => { const el = document.querySelector(sel); if (!el) return null; const r = el.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }; };
        const overlap = (a, b) => a && b && a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
        const inspector = box('[data-testid="inspector-pane"]');
        const timeline = box('.timeline-pane');
        const player = box('.player-card');
        const video = document.querySelector('video');
        return { inspector, timeline, player, videoVisible: !!video && video.getBoundingClientRect().width > 80,
          iOverT: overlap(inspector, timeline), pOverI: overlap(player, inspector), scrollW: document.documentElement.scrollWidth, innerW: window.innerWidth };
      });
      rec(`accept.8.${name}`, !!geo.inspector && !geo.iOverT && !geo.pOverI && geo.videoVisible,
        `inspector=${JSON.stringify(geo.inspector)} timeline=${JSON.stringify(geo.timeline)} 重叠(面板×时间线)=${geo.iOverT} 重叠(播放器×面板)=${geo.pOverI} 视频可见=${geo.videoVisible}`);
      await shot(page, `17-responsive-${name}`);
    }
    await page.setViewportSize({ width: 1600, height: 900 });
  }

  fs.writeFileSync(path.join(OUT, 'results-full.json'), JSON.stringify({ results, evidence, net }, null, 2));
  const passed = results.filter((r) => r.ok).length;
  log(`\nSUMMARY ${passed}/${results.length} passed`);
  if (results.some((r) => !r.ok)) log('FAILED: ' + results.filter((r) => !r.ok).map((r) => r.id).join(', '));
  await browser.close();
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
