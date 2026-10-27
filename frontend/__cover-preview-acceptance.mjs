/**
 * 封面预览比例修复（2026-10-21 窗口）headless 页面验收。
 *
 * 目标：封面资产是 9:16（真实项目 cover.jpg 1080×1920），播放器是横版。封面预览时
 * 播放器内部画布必须是 9:16，按播放器可用高度等比缩放并居中（两侧留黑边），
 * img 用 contain 完整显示——不拉伸、不裁切，改窗口大小比例不变；
 * 且封面预览与普通视频播放状态分开处理，不影响视频画布/预览比例/时间线。
 *
 * 真实前端（:3107 turbopack dev 的 /studio 页面代码）+ page.route mock /video-api
 * （封面沿用真实项目 timeline_b677b1639a1c 的既有 cover.jpg，不重新生成、不修改封面文件；
 * 脚本前后校验该文件 SHA256 与 mtime 不变）。
 *
 * 运行：node __cover-preview-acceptance.mjs（工作目录 frontend/）
 */
import { chromium } from "/domain/supoclip-medical-workbench/frontend/node_modules/.pnpm/playwright@1.59.1/node_modules/playwright/index.mjs";
import { readFileSync, statSync } from "node:fs";
import { createHash } from "node:crypto";

const PID = "timeline_b677b1639a1c";
const TASK = "autoedit_aaaabbbbcccc";
const ASSETS = "/tmp/clip-select-acceptance";
const COVER_FILE = "/domain/supoclip-medical-workbench/tasks/output/3804de059d2b47aa870611b5b87ea9c7/clips/clip-799c9018ac/cover.jpg";
const COVER_PATH = "output/3804de059d2b47aa870611b5b87ea9c7/clips/clip-799c9018ac/cover.jpg";
const REGISTRY_VERSION = "2026-10-21.2";
const coverBytes = readFileSync(COVER_FILE);
const sha = (buf) => createHash("sha256").update(buf).digest("hex");
const COVER_SHA_BEFORE = sha(coverBytes);
const COVER_MTIME_BEFORE = statSync(COVER_FILE).mtimeMs;
// 只读红线：本脚本只读真实项目文件；跑前跑后校验台账与封面元数据未被写入。
const ROOT = "/domain/supoclip-medical-workbench";
const LEDGER_FILE = `${ROOT}/tasks/projects/${PID}/clips.json`;
const REPLACE_FILE = `${ROOT}/tasks/output/3804de059d2b47aa870611b5b87ea9c7/clips/clip-799c9018ac/cover_replace.json`;
const guard = (path) => { const st = statSync(path); return { mtime: st.mtimeMs, size: st.size }; };
const GUARD_BEFORE = { ledger: guard(LEDGER_FILE), replace: guard(REPLACE_FILE) };

const TEMPLATES = [{ template_id: "cute", name: "可爱版", version: "1", example_path: "/studio/cover-templates/cute/example.svg", description: "柔和", sort: 1 }];

const CLIPS = [
  { id: "clip-d6a7789e82", name: "剪辑成片 01", seg: "第一问：免疫治疗最害怕的是什么？", dur: 8, cover: null },
  { id: "clip-0af9a07835", name: "剪辑成片 02", seg: "第二问：血压高要长期吃药吗？", dur: 10,
    cover: { cover_asset_id: "cover-clip-799c9018ac-v1", revision: 1, style_id: "realframe_v2", path: COVER_PATH } },
  { id: "clip-7c9e51d0aa", name: "剪辑成片 03", seg: "第三问：多久复查一次才合理？", dur: 9, cover: null },
];
const card = (c) => ({
  clip_id: c.id, name: c.name, title: c.name, topic: c.name, keywords: [], duration: c.dur,
  source_start: 0, source_end: c.dur, task_id: TASK, render_task_id: TASK, render_revision: 1,
  status: "ready", video_path: `output/${TASK}/${c.id}/clip_final.mp4`, cover: c.cover,
  task_history: [], segments: [{ start: 0, end: 2, text: c.seg }], cover_template: null,
});
const state = { putCalls: [], coverPosts: 0, templatePuts: [] };

const browser = await chromium.launch({ executablePath: "/opt/ms-playwright/chromium-1246/chrome-linux64/chrome", args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text().slice(0, 200)); });
page.on("pageerror", (e) => errors.push("PAGEERROR: " + e.message.slice(0, 300)));

await page.route("**/video-api/**", async (route) => {
  const url = new URL(route.request().url());
  const path = url.pathname;
  const method = route.request().method();
  try {
    if (path === `/video-api/studio/projects/${PID}/clips`) return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ project_id: PID, card_count: CLIPS.length, active_clip_id: null, cards: CLIPS.map(card) }) });
    if (path === "/video-api/studio/cover-templates" && method === "GET") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ registry_version: REGISTRY_VERSION, templates: TEMPLATES }) });
    if (/\/cover-templates\/[a-z_]+\/example\.svg$/.test(path)) return route.fulfill({ status: 200, contentType: "image/svg+xml", body: readFileSync("/domain/supoclip-medical-workbench/backend/assets/cover-templates/cute.svg") });
    if (/\/cover-template$/.test(path) && method === "PUT") {
      const body = JSON.parse(route.request().postData() || "{}");
      state.templatePuts.push({ clip_id: path.match(/(clip-[0-9a-f]+)/)?.[1], template_id: body.template_id ?? null });
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ project_id: PID, clip_id: "x", changed: true, cover_template: { template_id: body.template_id, registry_version: REGISTRY_VERSION, updated_at: "2026-10-21T03:00:00Z" } }) });
    }
    if (/\/clips\/(clip-[0-9a-f]+)\/cover$/.test(path) && method === "POST") {
      state.coverPosts += 1;
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ kind: "generated", style_id: "template:cute", idempotent: false, cover_revision: 2, replace_intro: false, qc: { passed: true }, cover_path: COVER_PATH }) });
    }
    if (/\/first-frame\.jpg$/.test(path)) {
      const cid = path.match(/(clip-[0-9a-f]+)/)[1];
      try { return route.fulfill({ status: 200, contentType: "image/jpeg", body: readFileSync(`${ASSETS}/frame-${cid}.jpg`) }); } catch { return route.fulfill({ status: 404, body: "no" }); }
    }
    if (path === `/video-api/studio/projects/${PID}/active-clip`) {
      const body = JSON.parse(route.request().postData() || "{}");
      state.putCalls.push(body.clip_id ?? null);
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ project_id: PID, active_clip_id: body.clip_id ?? null, changed: true }) });
    }
    if (path === `/video-api/studio/timeline/${PID}`) return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: PID, timeline: { ranges: CLIPS.map((c) => ({ id: c.id, source_start: 0, source_end: c.dur })) }, transcript: [] }) });
    if (path.startsWith("/video-api/studio/auto-edit/") && path.endsWith("/artifact")) {
      const raw = decodeURIComponent(url.searchParams.get("path") || "");
      if (/cover\.jpg$/.test(raw)) return route.fulfill({ status: 200, contentType: "image/jpeg", body: coverBytes });
      const cid = raw.split("/")[2] || "";
      try { return route.fulfill({ status: 200, contentType: "video/webm", body: readFileSync(`${ASSETS}/${cid}.webm`), headers: { "Accept-Ranges": "none" } }); } catch { return route.fulfill({ status: 404, body: "no" }); }
    }
    return route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "mock-404" }) });
  } catch (e) {
    return route.fulfill({ status: 500, body: String(e) });
  }
});

const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: Boolean(ok), detail: String(detail).slice(0, 200) }); console.log(`${ok ? "PASS" : "FAIL"} ${name}${ok ? "" : " | " + detail}`); };
const waitFor = (ms) => page.waitForTimeout(ms);
const shot = (name) => page.screenshot({ path: `/domain/supoclip-medical-workbench/docs/screenshots/${name}` });

const geometry = () => page.evaluate(() => {
  const box = (sel) => { const el = document.querySelector(sel); if (!el) return null; const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height, top: r.top, bottom: r.bottom, left: r.left, right: r.right }; };
  const canvas = document.querySelector(".cover-frame-canvas");
  const img = canvas?.querySelector("img");
  const stage = document.querySelector(".player-stage");
  const frame = document.querySelector(".preview-frame");
  const video = document.querySelector(".preview-frame video");
  const vcs = video ? getComputedStyle(video) : null;
  const viewerCanvas = document.querySelector(".transform-canvas");
  const ics = img ? getComputedStyle(img) : null;
  return {
    mode: stage?.getAttribute("data-preview-mode") ?? null,
    stage: box(".player-stage"), frame: box(".preview-frame"), canvas: box(".cover-frame-canvas"),
    img: img ? { ...box(".cover-frame-canvas img"), natural: [img.naturalWidth, img.naturalHeight], objectFit: ics.objectFit, complete: img.complete } : null,
    video: video ? { ...box(".preview-frame video"), objectFit: vcs.objectFit, src: video.getAttribute("src"), readyState: video.readyState } : null,
    viewerCanvas: box(".transform-canvas"),
    viewerFrame: box(".viewer-frame"),
    badge: box(".cover-frame-badge"),
  };
});
const near = (a, b, tol) => Math.abs(a - b) <= tol;

try {
  await page.goto(`http://127.0.0.1:3107/studio?source_id=${PID}`, { waitUntil: "domcontentloaded", timeout: 90000 });
  await page.waitForSelector(".asset-card.rendered-card", { timeout: 90000 });
  await waitFor(2000);
  await page.$$eval(".asset-card.rendered-card", (ns) => ns.find((n) => n.textContent.includes("剪辑成片 02")).click());
  await waitFor(1500);

  // ---- S1 普通播放态：封面层不存在，视频画布比例不受影响 ----
  let g = await geometry();
  check("S1 普通播放：data-preview-mode=video", g.mode === "video", g.mode);
  check("S1 普通播放：无封面层", g.canvas === null && g.img === null, JSON.stringify({ c: g.canvas, i: g.img }));
  check("S1 普通播放：视频画布仍是 9:16 变换画布（既有行为未改）", Boolean(g.viewerCanvas) && near(g.viewerCanvas.w / g.viewerCanvas.h, 9 / 16, 0.004), JSON.stringify(g.viewerCanvas && [g.viewerCanvas.w, g.viewerCanvas.h]));
  check("S1 普通播放：video 位于变换画布内（fill 语义不变）", Boolean(g.video && g.viewerCanvas) && near(g.video.w, g.viewerCanvas.w, 1) && near(g.video.h, g.viewerCanvas.h, 1) && g.video.objectFit === "fill", JSON.stringify(g.video && [g.video.w, g.video.h, g.video.objectFit]));
  const videoSrcBefore = g.video?.src || null;
  const viewerBefore = g.viewerCanvas ? { ...g.viewerCanvas } : null;
  await shot("cover-preview-1-video.png");

  // ---- S2 激活封面预览（真实封面 1080×1920，经既有版型制作通道）----
  await page.evaluate(() => {
    const tab = [...document.querySelectorAll("button,[role=tab]")].find((b) => b.textContent.trim() === "封面");
    if (tab) tab.click();
  });
  await waitFor(1000);
  const selected = await page.evaluate(() => {
    const t = [...document.querySelectorAll("button")].find((b) => b.textContent.includes("可爱版"));
    if (t) { t.click(); return true; } return false;
  });
  check("S2 选中版型卡（触发通道前提）", selected === true);
  await waitFor(600);
  const generated = await page.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find((x) => x.textContent.includes("制作封面"));
    if (b) { b.click(); return b.textContent.trim(); } return null;
  });
  check("S2 点击制作封面（既有通道）", generated === "使用可爱版制作封面", generated);
  await page.waitForSelector('[data-testid="cover-frame-view"]', { timeout: 15000 });
  await waitFor(1200);
  g = await geometry();
  check("S2 封面预览：data-preview-mode=cover", g.mode === "cover", g.mode);
  check("S2 封面层画布存在且 img 已解码", Boolean(g.canvas && g.img && g.img.natural?.[0] === 1080 && g.img.natural?.[1] === 1920 && g.img.complete), JSON.stringify(g.img && { natural: g.img.natural, complete: g.img.complete }));
  const canvasAspect = g.canvas ? g.canvas.w / g.canvas.h : 0;
  check("S2 画布比例=9:16（±0.004）", near(canvasAspect, 9 / 16, 0.004), canvasAspect.toFixed(4));
  check("S2 画布按可用高度缩放（h≈画面区高）", g.canvas && g.frame && near(g.canvas.h, g.frame.h, 1), JSON.stringify({ canvasH: g.canvas?.h, frameH: g.frame?.h }));
  check("S2 画布完整落在播放器内（不溢出、不裁切）", g.canvas && g.frame
    && g.canvas.top >= g.frame.top - 0.5 && g.canvas.bottom <= g.frame.bottom + 0.5
    && g.canvas.left >= g.frame.left - 0.5 && g.canvas.right <= g.frame.right + 0.5,
    JSON.stringify({ canvas: g.canvas && [g.canvas.top, g.canvas.bottom, g.canvas.left, g.canvas.right], frame: g.frame && [g.frame.top, g.frame.bottom, g.frame.left, g.frame.right] }));
  check("S2 画布水平居中（两侧黑边近似相等 ±1.5）", g.canvas && g.frame
    && near(g.canvas.left - g.frame.left, g.frame.right - g.canvas.right, 1.5),
    JSON.stringify({ left: g.canvas?.left - g.frame?.left, right: g.frame?.right - g.canvas?.right }));
  check("S2 img 撑满画布（contain 不产生内部留白）", g.img && near(g.img.w, g.canvas.w, 1) && near(g.img.h, g.canvas.h, 1)
    && near(g.img.left, g.canvas.left, 1) && near(g.img.top, g.canvas.top, 1), JSON.stringify({ img: g.img && [g.img.w, g.img.h], canvas: g.canvas && [g.canvas.w, g.canvas.h] }));
  check("S2 img object-fit=contain（完整显示、不拉伸）", g.img?.objectFit === "contain", g.img?.objectFit);
  const imgAspect = g.img ? g.img.natural[0] / g.img.natural[1] : 0;
  check("S2 封面自然比=画布比（无裁切：顶部文字/底部边缘全在框内）", near(imgAspect, canvasAspect, 0.004), JSON.stringify({ imgAspect: imgAspect.toFixed(4), canvasAspect: canvasAspect.toFixed(4) }));
  check("S2 封面预览不改视频源（时间线/源视频未动）", g.video?.src === videoSrcBefore, JSON.stringify({ before: videoSrcBefore, now: g.video?.src }));
  check("S2 封面预览不改视频画布尺寸/比例（frame 仍铺满）", g.frame && near(g.frame.w, g.stage.w, 1) && near(g.frame.h, g.stage.h, 1));
  check("S2 封面预览期间视频画布几何与普通播放一致（独立叠加层）", Boolean(g.viewerCanvas && viewerBefore) && near(g.viewerCanvas.w, viewerBefore.w, 1) && near(g.viewerCanvas.h, viewerBefore.h, 1) && near(g.viewerCanvas.top, viewerBefore.top, 1), JSON.stringify({ before: viewerBefore && [viewerBefore.w, viewerBefore.h], now: g.viewerCanvas && [g.viewerCanvas.w, g.viewerCanvas.h] }));
  await shot("cover-preview-2-cover.png");
  // 供像素级核对（顶部文字/四周边缘/人物是否完整可见）：记录画布在视口中的实际位置
  await page.evaluate(() => { /* noop: 保持与截图同帧 */ });
  const w = await import("node:fs");
  w.writeFileSync("/tmp/cover-preview-geometry.json", JSON.stringify({ canvas: g.canvas, frame: g.frame, img: { natural: g.img?.natural }, viewport: page.viewportSize(), shot: "/domain/supoclip-medical-workbench/docs/screenshots/cover-preview-2-cover.png" }));

  // ---- S3 改窗口大小：画布仍 9:16、仍按高度缩放并居中 ----
  for (const [w, h] of [[1180, 800], [1440, 1150]]) {
    await page.setViewportSize({ width: w, height: h });
    await waitFor(1200);
    g = await geometry();
    const a = g.canvas ? g.canvas.w / g.canvas.h : 0;
    check(`S3 ${w}×${h}：画布仍 9:16`, near(a, 9 / 16, 0.004), a.toFixed(4));
    check(`S3 ${w}×${h}：仍按可用高度缩放且完整可见`, g.canvas && g.frame && near(g.canvas.h, g.frame.h, 1)
      && g.canvas.top >= g.frame.top - 0.5 && g.canvas.bottom <= g.frame.bottom + 0.5, JSON.stringify({ canvasH: g.canvas?.h, frameH: g.frame?.h }));
    check(`S3 ${w}×${h}：仍居中`, g.canvas && g.frame && near(g.canvas.left - g.frame.left, g.frame.right - g.canvas.right, 1.5));
    check(`S3 ${w}×${h}：img 仍完整撑满画布`, g.img && near(g.img.w, g.canvas.w, 1) && near(g.img.h, g.canvas.h, 1));
  }
  await shot("cover-preview-3-resized.png");

  // ---- S4 回到普通播放：封面层不残留，视频画布/时间线照旧 ----
  await page.$$eval(".asset-card.rendered-card", (ns) => ns.find((n) => n.textContent.includes("剪辑成片 01")).click());
  await waitFor(1500);
  g = await geometry();
  check("S4 切回无封面切片：封面层消失", g.canvas === null && g.img === null);
  check("S4 切回普通播放：data-preview-mode=video", g.mode === "video", g.mode);
  check("S4 普通播放画布未被封面层改动（9:16 变换画布 + video fill 语义还原）", Boolean(g.viewerCanvas) && near(g.viewerCanvas.w / g.viewerCanvas.h, 9 / 16, 0.004)
    && Boolean(g.video) && near(g.video.w, g.viewerCanvas.w, 1) && near(g.video.h, g.viewerCanvas.h, 1) && g.video.objectFit === "fill"
    && (g.video.src || "").includes("clip-d6a7789e82"), JSON.stringify({ canvas: g.viewerCanvas && [g.viewerCanvas.w, g.viewerCanvas.h], video: g.video && [g.video.w, g.video.h, g.video.objectFit, g.video.src] }));
  check("S4 时间线/视频源未因封面预览改变（PUT 仍只有卡片选择）", JSON.stringify(state.putCalls) === JSON.stringify(["clip-0af9a07835", "clip-d6a7789e82"]), JSON.stringify(state.putCalls));
  await shot("cover-preview-4-back-to-video.png");

  // ---- 封面文件零写入 ----
  const shaAfter = sha(readFileSync(COVER_FILE));
  check("封面文件未被重新生成/修改（SHA256+mtime 不变）", shaAfter === COVER_SHA_BEFORE && statSync(COVER_FILE).mtimeMs === COVER_MTIME_BEFORE, `sha=${shaAfter.slice(0, 12)}`);
  const guardAfter = { ledger: guard(LEDGER_FILE), replace: guard(REPLACE_FILE) };
  check("真实台账与封面元数据零写入（clips.json / cover_replace.json mtime+size 不变）",
    JSON.stringify(guardAfter) === JSON.stringify(GUARD_BEFORE), JSON.stringify({ before: GUARD_BEFORE, after: guardAfter }));

  const benign = (e) => /video-api|Failed to fetch|favicon|404|mock-404|401|studio_auth/.test(e);
  const hardErrors = errors.filter((e) => !benign(e));
  check("无新增页面代码错误", hardErrors.length === 0, JSON.stringify(hardErrors.slice(0, 4)));
  console.log("SUMMARY:", JSON.stringify({ passed: results.filter((r) => r.ok).length, total: results.length, coverPosts: state.coverPosts, putCalls: state.putCalls }));
  await browser.close();
  process.exit(results.every((r) => r.ok) ? 0 : 1);
} catch (error) {
  console.log("SCRIPT ERROR:", error && (error.stack || error.message || String(error)));
  await page.screenshot({ path: "/tmp/cover-preview-error.png" }).catch(() => {});
  await browser.close();
  process.exit(2);
}
