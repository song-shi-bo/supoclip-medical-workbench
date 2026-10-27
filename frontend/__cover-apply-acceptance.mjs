/**
 * 封面区尺寸 + 「应用」渲染任务 headless 页面验收（2026-10-21）。
 *
 * 验收口径（对应用户需求）：
 *  1. 右侧封面缩略图是 ~80×142 的 9:16 容器，封面 contain 完整显示（不拉伸、不裁切）；
 *     点击缩略图才展开「应用 / 删除」；
 *  2. 点「应用」= 调真实渲染任务接口（POST /studio/projects/{p}/clips/{c}/tasks，
 *     task_type=APPLY_COVER），不是同步假实现；
 *  3. 点击后立刻出现「正在应用/渲染中」+ 阶段/进度；完成后出现「已应用到成片」；
 *  4. 渲染失败展示原因（全局状态条），保留待应用状态、不假报成功；
 *  5. 已被成片使用的封面不给删除入口。
 *
 * 真实前端（:3107）+ page.route mock /video-api；封面图用真实项目既有 cover.jpg
 * （1080×1920），脚本前后校验真实台账与封面文件未被写入。
 *
 * 运行：node __cover-apply-acceptance.mjs（工作目录 frontend/）
 */
import { chromium } from "/domain/supoclip-medical-workbench/frontend/node_modules/.pnpm/playwright@1.59.1/node_modules/playwright/index.mjs";
import { readFileSync, statSync, mkdirSync, existsSync } from "node:fs";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";

const PID = "timeline_b677b1639a1c";
const TASK = "autoedit_aaaabbbbcccc";
const ASSETS = "/tmp/clip-select-acceptance";
const ROOT = "/domain/supoclip-medical-workbench";
const COVER_FILE = `${ROOT}/tasks/output/3804de059d2b47aa870611b5b87ea9c7/clips/clip-d6a7789e82/cover.jpg`;
const COVER_PATH = "output/3804de059d2b47aa870611b5b87ea9c7/clips/clip-d6a7789e82/cover.jpg";
const REGISTRY_VERSION = "2026-10-21.2";
const coverBytes = readFileSync(COVER_FILE);
const sha = (buf) => createHash("sha256").update(buf).digest("hex");
const COVER_SHA_BEFORE = sha(coverBytes);
const COVER_MTIME_BEFORE = statSync(COVER_FILE).mtimeMs;
const TEMPLATES = [{ template_id: "cute", name: "可爱版", version: "1", example_path: "/studio/cover-templates/cute/example.svg", description: "柔和", sort: 1 }];

// /tmp 是易失的 tmpfs：验收资产（首帧 jpg / 播放体 webm）缺失时自动重建，脚本可随时重跑。
const FFMPEG = "/domain/dsh-home/bin/ffmpeg";
// 首帧来源统一用 COVER_FILE（真实项目既有封面，肯定存在；只读不写）。
function ensureAssets() {
  mkdirSync(ASSETS, { recursive: true });
  for (const clip of ["clip-d6a7789e82", "clip-0af9a07835", "clip-7c9e51d0aa"]) {
    const frame = `${ASSETS}/frame-${clip}.jpg`;
    if (!existsSync(frame)) {
      execFileSync(FFMPEG, ["-loglevel", "error", "-y", "-ss", "0.3", "-i", COVER_FILE, "-frames:v", "1", "-q:v", "3", frame],
        { stdio: ["ignore", "ignore", "pipe"] });
    }
    const webm = `${ASSETS}/${clip}.webm`;
    if (!existsSync(webm)) {
      execFileSync(FFMPEG, ["-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=360x640:rate=15:duration=10",
        "-c:v", "libvpx", "-b:v", "400k", "-pix_fmt", "yuv420p", webm],
        { stdio: ["ignore", "ignore", "pipe"] });
    }
  }
}
const GUARD = [COVER_FILE, `${ROOT}/tasks/projects/${PID}/clips.json`];
const guardState = () => Object.fromEntries(GUARD.map((p) => {
  const st = statSync(p);
  return [p, { mtime: st.mtimeMs, size: st.size, sha: p.endsWith(".jpg") ? sha(readFileSync(p)) : null }];
}));
const BEFORE = guardState();
ensureAssets();

const CLIPS = [
  { id: "clip-d6a7789e82", name: "剪辑成片 01", seg: "第一问：免疫治疗最害怕的是什么？", dur: 8 },
  { id: "clip-0af9a07835", name: "剪辑成片 02", seg: "第二问：血压高要长期吃药吗？", dur: 10 },
];
const state = {
  pending: { "clip-d6a7789e82": true, "clip-0af9a07835": true },
  applied: {},
  taskByClip: {},
  clipByTask: {},
  polls: {},
  taskBodies: [],
  failClip: "clip-0af9a07835",      // 02 的渲染任务会失败，用来验证「失败不假报成功」
};

const cardFor = (c) => ({
  clip_id: c.id, name: c.name, title: c.name, topic: c.name, keywords: [], duration: c.dur,
  source_start: 0, source_end: c.dur, task_id: TASK, render_task_id: TASK, render_revision: 1,
  status: "ready", video_path: `output/${TASK}/${c.id}/clip_final.mp4`,
  cover: { path: COVER_PATH, revision: 1, cover_asset_id: "cover-1" },
  task_history: [], segments: [{ start: 0, end: 2, text: c.seg }],
  cover_template: { template_id: "cute", registry_version: "2026-10-21.2", updated_at: "t" },
  cover_workflow: {
    has_cover: true,
    pending: Boolean(state.pending[c.id]) && !state.applied[c.id],
    applied: Boolean(state.applied[c.id]),
    revision: 2,
    style: "template:cute",
  },
});
const cardsPayload = () => ({ project_id: PID, card_count: CLIPS.length, active_clip_id: null, cards: CLIPS.map(cardFor) });

const errors = [];
const browser = await chromium.launch({ executablePath: "/opt/ms-playwright/chromium-1246/chrome-linux64/chrome", args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text().slice(0, 200)); });
page.on("pageerror", (e) => errors.push("PAGEERROR: " + e.message.slice(0, 300)));

await page.route("**/video-api/**", async (route) => {
  const url = new URL(route.request().url());
  const path = url.pathname;
  const method = route.request().method();
  try {
    if (path === `/video-api/studio/projects/${PID}/clips`) {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(cardsPayload()) });
    }
    if (path === "/video-api/studio/cover-templates") {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ registry_version: "2026-10-21.2", templates: [] }) });
    }
    // 应用 = 真实渲染任务接口
    const createTask = path.match(/^\/video-api\/studio\/projects\/[^/]+\/clips\/(clip-[0-9a-f]+)\/tasks$/);
    if (createTask && method === "POST") {
      const body = JSON.parse(route.request().postData() || "{}");
      const clipId = createTask[1];
      state.taskBodies.push({ clip_id: clipId, ...body });
      const taskId = `coverapply_${clipId.replace(/[^a-z0-9]/gi, "").slice(-8).padStart(8, "0")}`;
      state.taskByClip[clipId] = taskId;
      state.clipByTask[taskId] = clipId;
      state.polls[clipId] = 0;
      return route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({
        task_id: taskId, task_type: "APPLY_COVER", project_id: PID, clip_id: clipId,
        status: "queued", stage: "任务已受理", created_at: "t" }) });
    }
    const taskStatus = path.match(/^\/video-api\/studio\/tasks\/(coverapply_[a-z0-9]+)$/);
    if (taskStatus && method === "GET") {
      const taskId = taskStatus[1];
      const clipId = state.clipByTask[taskId] || "";
      state.polls[clipId] = (state.polls[clipId] || 0) + 1;
      if (clipId === state.failClip) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
          task_id: taskId, task_type: "APPLY_COVER", clip_id: clipId, status: "failed",
          stage: "失败", progress: 1,
          error: "E_COVER_RENDER_FAILED: 片头渲染失败，已保留原成片" }) });
      }
      if (state.polls[clipId] <= 3) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
          task_id: taskId, task_type: "APPLY_COVER", clip_id: clipId, status: "processing",
          stage: "渲染 0.8s 封面片头并拼接正片", progress: 0.45 }) });
      }
      state.applied[clipId] = true;
      state.pending[clipId] = false;
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        task_id: taskId, task_type: "APPLY_COVER", clip_id: clipId, status: "completed",
        stage: "已完成", progress: 1,
        result: { applied: true, idempotent: false, intro_duration_s: 0.8,
                  card: cardsPayload().cards.find((card) => card.clip_id === clipId) } }) });
    }
    // 删除：已应用 → 后端拒绝（防御性断言；UI 本来就不给删除入口）
    const del = path.match(/^\/video-api\/studio\/auto-edit\/[^/]+\/clips\/(clip-[0-9a-f]+)\/cover-template\/delete$/);
    if (del && method === "POST") {
      if (state.applied[del[1]]) {
        return route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: "该版型封面已应用；如需更换请制作新封面" }) });
      }
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ deleted_revision: 2, current_revision: 1 }) });
    }
    const coverHistory = path.match(/^\/video-api\/studio\/projects\/[^/]+\/clips\/(clip-[0-9a-f]+)\/cover-history$/);
    if (coverHistory && method === "GET") {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        project_id: PID, clip_id: coverHistory[1],
        revisions: [{ revision: 2, kind: "generated", style_id: "template:cute", created_at: "t" }] }) });
    }
    if (/\/first-frame\.jpg$/.test(path)) {
      const cid = path.match(/(clip-[0-9a-f]+)/)[1];
      try { return route.fulfill({ status: 200, contentType: "image/jpeg", body: readFileSync(`${ASSETS}/frame-${cid}.jpg`) }); } catch { return route.fulfill({ status: 404, body: "no" }); }
    }
    if (path === `/video-api/studio/projects/${PID}/active-clip`) {
      const body = JSON.parse(route.request().postData() || "{}");
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ project_id: PID, active_clip_id: body.clip_id ?? null, changed: true }) });
    }
    if (path === `/video-api/studio/timeline/${PID}`) {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: PID, timeline: { ranges: CLIPS.map((c) => ({ id: c.id, source_start: 0, source_end: c.dur })) }, transcript: [] }) });
    }
    if (path.startsWith("/video-api/studio/auto-edit/") && path.endsWith("/artifact")) {
      const raw = decodeURIComponent(url.searchParams.get("path") || "");
      if (/cover\.jpg$/.test(raw)) return route.fulfill({ status: 200, contentType: "image/jpeg", body: coverBytes });
      const cid = raw.split("/")[2] || "";
      try {
        const body = readFileSync(`${ASSETS}/${cid}.webm`);
        return route.fulfill({ status: 200, contentType: "video/webm", body, headers: { "Accept-Ranges": "none" } });
      } catch (err) {
        console.log("MOCK MISS artifact:", raw, "->", String(err).slice(0, 80));
        return route.fulfill({ status: 404, body: "no" });
      }
    }
    return route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "mock-404" }) });
  } catch (e) {
    return route.fulfill({ status: 500, body: String(e) });
  }
});

const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: Boolean(ok), detail: String(detail).slice(0, 200) }); console.log(`${ok ? "PASS" : "FAIL"} ${name}${ok ? "" : " | " + detail}`); };
const wait = (ms) => page.waitForTimeout(ms);
const shot = (name) => page.screenshot({ path: `${ROOT}/docs/screenshots/${name}` });

const openCoverTab = async () => {
  await page.evaluate(() => {
    const tab = [...document.querySelectorAll("button,[role=tab]")].find((b) => b.textContent.trim() === "封面");
    if (tab) tab.click();
  });
  await wait(900);
};
const selectClip = async (name) => {
  await page.$$eval(".asset-card.rendered-card", (ns, target) => {
    const card = ns.find((n) => n.textContent.includes(target));
    if (card) card.click();
  }, name);
  await wait(1200);
};
const thumbGeo = () => page.evaluate(() => {
  const box = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; };
  const wrap = document.querySelector('[data-testid="cover-workspace-image"]');
  const img = wrap ? wrap.querySelector("img") : null;
  const cs = img ? getComputedStyle(img) : null;
  return {
    wrap: wrap ? { ...box(wrap), client: [wrap.clientWidth, wrap.clientHeight] } : null,
    img: img ? { ...box(img), natural: [img.naturalWidth, img.naturalHeight], objectFit: cs.objectFit, complete: img.complete } : null,
    actions: Boolean(document.querySelector('[data-testid="cover-workspace-actions"]')),
    progress: (() => { const p = document.querySelector('[data-testid="cover-workspace-progress"]'); return p ? { text: p.textContent, percent: p.getAttribute("data-percent") } : null; })(),
    applied: document.querySelector('[data-testid="cover-workspace-image"]')?.getAttribute("data-applied") === "true",
    pending: document.querySelector('[data-testid="cover-workspace-image"]')?.getAttribute("data-pending") === "true",
    deleteBtn: Boolean(document.querySelector('[data-testid="cover-workspace-delete"]')),
    workspaceH: (() => { const el = document.querySelector('[data-testid="cover-workspace"]'); return el ? Math.round(el.getBoundingClientRect().height) : null; })(),
    gapToTemplates: (() => {
      const ws = document.querySelector('[data-testid="cover-workspace"]');
      const tpl = document.querySelector('[data-testid="cover-template-grid"]');
      if (!ws || !tpl) return null;
      return Math.round(tpl.getBoundingClientRect().top - ws.getBoundingClientRect().bottom);
    })(),
    statusTexts: (() => {
      const pane = document.querySelector(".transcript-pane") || document.body;
      return { applied: pane.textContent.includes("已应用到成片"), noDelete: pane.textContent.includes("不能删除"), hint: Boolean(document.querySelector('[data-testid="cover-workspace-applied-hint"], [data-testid="cover-workspace-applied-inline"]')) };
    })(),
    message: document.querySelector('[data-testid="studio-message"]')?.textContent || null,
  };
});
const near = (a, b, tol) => Math.abs(a - b) <= tol;

try {
  await page.goto(`http://127.0.0.1:3107/studio?source_id=${PID}`, { waitUntil: "domcontentloaded", timeout: 90000 });
  await page.waitForSelector(".asset-card.rendered-card", { timeout: 90000 });
  await wait(1500);
  await selectClip("剪辑成片 01");
  await openCoverTab();

  // ---- S1 缩略图尺寸与完整显示 ----
  let g = await thumbGeo();
  check("S1 缩略图容器约 80×142", Boolean(g.wrap) && near(g.wrap.w, 80, 2) && near(g.wrap.h, 142, 2), JSON.stringify(g.wrap));
  check("S1 缩略图是 9:16（±0.01）", Boolean(g.wrap) && near(g.wrap.w / g.wrap.h, 9 / 16, 0.01), g.wrap && (g.wrap.w / g.wrap.h).toFixed(4));
  check("S1 封面 img contain 完整显示（1080×1920）", Boolean(g.img) && g.img.objectFit === "contain" && g.img.complete && g.img.natural[0] === 1080 && g.img.natural[1] === 1920, JSON.stringify(g.img));
  check("S1 img 与容器内容盒同尺寸（无裁切/拉伸留白）", Boolean(g.img && g.wrap) && near(g.img.w, g.wrap.client[0], 1) && near(g.img.h, g.wrap.client[1], 1), JSON.stringify({ img: g.img && [g.img.w, g.img.h], client: g.wrap && g.wrap.client }));
  check("S1 未点击缩略图前不显示操作条", g.actions === false);
  // ---- S1b 卡片缩略图不得随信息栏被拉长（2026-10-22 用户报「选中后卡片拉长」）----
  const cardGeo = await page.evaluate(() => {
    const card = document.querySelector(".asset-card.rendered-card")?.getBoundingClientRect();
    const cov = document.querySelector(".rendered-cover")?.getBoundingClientRect();
    return card && cov ? { card: [card.width, card.height], cover: [cov.width, cov.height] } : null;
  });
  // 确定性复现：往信息栏注入 120px 高内容，把卡片行撑高（等价于真实卡片的长标题/警告）
  await page.evaluate(() => {
    const info = document.querySelector(".asset-card.rendered-card .rendered-info");
    if (info) { const d = document.createElement("div"); d.style.height = "120px"; info.appendChild(d); }
  });
  await wait(300);
  const cardGeo2 = await page.evaluate(() => {
    const card = document.querySelector(".asset-card.rendered-card")?.getBoundingClientRect();
    const cov = document.querySelector(".rendered-cover")?.getBoundingClientRect();
    return card && cov ? { card: [card.width, card.height], cover: [cov.width, cov.height] } : null;
  });
  check("S1b 信息栏撑高后卡片变高（复现前提成立）", Boolean(cardGeo2) && cardGeo2.card[1] > cardGeo.cover[1] + 60, JSON.stringify(cardGeo2));
  check("S1b 缩略图保持 88×156（9:16，不被拉长）", Boolean(cardGeo2) && near(cardGeo2.cover[0], 88, 2) && near(cardGeo2.cover[1], cardGeo2.cover[0] * 16 / 9, 3), JSON.stringify(cardGeo2.cover));
  // ---- S2 点击缩略图 → 应用/删除 ----
  await page.click('[data-testid="cover-workspace-image"]');
  await wait(400);
  g = await thumbGeo();
  check("S2 点击缩略图出现应用/删除", g.actions === true && g.deleteBtn === true && Boolean(await page.$('[data-testid="cover-workspace-apply"]')));

  // ---- S3 点击应用 → 任务 + 进度 + 完成 ----
  await page.click('[data-testid="cover-workspace-apply"]');
  await page.waitForSelector('[data-testid="cover-workspace-progress"]', { timeout: 8000 });
  g = await thumbGeo();
  check("S3 应用走了真实渲染任务接口（APPLY_COVER）", state.taskBodies.length === 1 && state.taskBodies[0].task_type === "APPLY_COVER" && state.taskBodies[0].clip_id === "clip-d6a7789e82", JSON.stringify(state.taskBodies));
  check("S3 立即显示渲染中阶段与进度", Boolean(g.progress) && g.progress.text.includes("渲染 0.8s 封面片头") && /4[0-9]%|5[0-9]%/.test(g.progress.text), JSON.stringify(g.progress));
  await shot("cover-apply-2-progress.png");
  await page.waitForFunction(() => document.querySelector('[data-testid="cover-workspace-image"]')?.getAttribute("data-applied") === "true", { timeout: 12000 });
  await wait(500);
  g = await thumbGeo();
  check("S3 完成后内部转已应用（数据标记，不再展示状态面板）", g.applied === true && g.pending === false);
  check("S3 已应用/待应用状态面板与提示文案已移除", g.statusTexts.applied === false && g.statusTexts.noDelete === false && g.statusTexts.hint === false, JSON.stringify(g.statusTexts));
  check("S3 封面区无空白占位（高度 ≤ 220px、与版型卡间距 ≤ 90px）", (g.workspaceH ?? 999) <= 220 && (g.gapToTemplates === null || Math.abs(g.gapToTemplates) <= 90), JSON.stringify({ h: g.workspaceH, gap: g.gapToTemplates }));
  // ---- S3c 封面页只保留 图片 + 版型卡片：画面/裁切重构图等不再出现在封面页（已移到效果页）----
  const paneText = await page.evaluate(() => (document.querySelector(".transcript-pane") || document.body).textContent || "");
  check("S3c 封面页无「画面设置」面板（aria-label=画面设置面板）", await page.$('[aria-label="画面设置面板"]') === null);
  for (const word of ["预览比例", "裁切/重构图", "重置变换", "片段缩放"]) {
    check(`S3c 封面页无「${word}」`, !paneText.includes(word));
  }
  // 功能迁移而非删除：效果页仍能找到画面设置面板
  await page.evaluate(() => {
    const tab = [...document.querySelectorAll("button,[role=tab]")].find((b) => b.textContent.trim() === "效果");
    if (tab) tab.click();
  });
  await wait(900);
  check("S3c 画面设置面板已迁移到「效果」页（功能保留）", await page.$('[aria-label="画面设置面板"]') !== null);
  await page.evaluate(() => {
    const tab = [...document.querySelectorAll("button,[role=tab]")].find((b) => b.textContent.trim() === "封面");
    if (tab) tab.click();
  });
  await wait(600);
  check("S3 完成提示可见（片头已加入成片）", (g.message || "").includes("片头已加入成片"), g.message);
  check("S3 已应用的封面不再给删除入口", g.deleteBtn === false);
  check("S3 缩略图尺寸不被应用流程改变", near(g.wrap.w, 80, 2) && near(g.wrap.h, 142, 2));
  const videoSrcAfterApply = await page.$eval(".preview-frame video", (v) => v.getAttribute("src")).catch(() => null);
  check("S3 成片 URL 已换版本（否则会继续播缓存的旧画面）", (videoSrcAfterApply || "").includes("&v="), videoSrcAfterApply);
  check("S3 应用后封面预览层已退出（画面不再是静态封面）", await page.$('[data-testid="cover-frame-view"]') === null);

  // ---- S3b 点播放：画面必须动起来（不能一直停在封面）----
  await page.click('.play-button');
  const advanced = await page.waitForFunction(() => {
    const v = document.querySelector('.preview-frame video');
    return v && v.currentTime > 0.5 && !v.paused;
  }, { timeout: 10000 }).then(() => true).catch(() => false);
  check("S3b 点播放后视频真实播放（currentTime 前进）", advanced === true);
  check("S3b 播放中封面预览层不出现（不会一直显示封面）", await page.$('[data-testid="cover-frame-view"]') === null);
  const t = await page.$eval('.preview-frame video', (v) => ({ t: v.currentTime, src: v.currentSrc, err: v.error && `${v.error.code}:${v.error.message}`, paused: v.paused, rs: v.readyState })).catch(() => null);
  console.log('S3b video state:', JSON.stringify(t));
  console.log('S3b placeholder:', await page.$('.video-error-placeholder') !== null);
  await shot("cover-apply-5-playing.png");
  await page.click('.play-button');   // 暂停，避免影响后续场景
  await shot("cover-apply-3-applied.png");

  // ---- S4 失败场景：02 的渲染任务失败 ----
  await selectClip("剪辑成片 02");
  await openCoverTab();
  await page.click('[data-testid="cover-workspace-image"]');
  await wait(300);
  await page.click('[data-testid="cover-workspace-apply"]');
  await wait(2500);
  g = await thumbGeo();
  check("S4 失败后仍是「待应用」（不假报成功）", g.pending === true && g.applied === false);
  check("S4 失败原因可见（含后端错误码）", (g.message || "").includes("E_COVER_RENDER_FAILED") && (g.message || "").includes("原成片保持不变"), g.message);
  check("S4 失败任务确实调用了一次渲染任务", state.taskBodies.filter((b) => b.clip_id === "clip-0af9a07835").length === 1);
  await shot("cover-apply-4-failed.png");

  // ---- S5 真实项目零写入 ----
  const after = guardState();
  check("S5 真实台账/封面文件零写入（mtime+sha 不变）", JSON.stringify(after) === JSON.stringify(BEFORE), JSON.stringify(after).slice(0, 200));

  // ---- S1c 封面历史是覆盖层：打开「查看历史」不得把卡片撑高/撑出空白 ----
  const cardH0 = await page.$eval(".asset-card.rendered-card", (el) => el.getBoundingClientRect().height);
  await page.$eval(".asset-card.rendered-card .rendered-menu-toggle", (el) => el.click());
  await wait(400);
  await page.$$eval(".rendered-menu button", (ns) => { const b = ns.find((x) => x.textContent.includes("查看历史")); if (b) b.click(); });
  await wait(600);
  const cardH1 = await page.$eval(".asset-card.rendered-card", (el) => el.getBoundingClientRect().height);
  const hist = await page.$(".rendered-history");
  check("S1c 打开封面历史显示浮层", hist !== null);
  check("S1c 历史浮层不改变卡片高度（不再撑出大空白）", Math.abs(cardH1 - cardH0) <= 2, JSON.stringify({ before: Math.round(cardH0), after: Math.round(cardH1) }));
  await page.$eval(".rendered-history header button", (el) => el.click());
  await wait(300);
  check("S1c 关闭历史后卡片高度不变", Math.abs(await page.$eval(".asset-card.rendered-card", (el) => el.getBoundingClientRect().height) - cardH0) <= 2);
  await shot("cover-apply-1-thumb.png");


  const benign = (e) => /video-api|Failed to fetch|favicon|404|mock-404|401/.test(e);
  const hardErrors = errors.filter((e) => !benign(e));
  check("无新增页面代码错误", hardErrors.length === 0, JSON.stringify(hardErrors.slice(0, 4)));
  console.log("SUMMARY:", JSON.stringify({ passed: results.filter((r) => r.ok).length, total: results.length, taskBodies: state.taskBodies }));
  await browser.close();
  process.exit(results.every((r) => r.ok) ? 0 : 1);
} catch (error) {
  console.log("SCRIPT ERROR:", error && (error.stack || error.message || String(error)));
  await page.screenshot({ path: "/tmp/cover-apply-error.png" }).catch(() => {});
  await browser.close();
  process.exit(2);
}
