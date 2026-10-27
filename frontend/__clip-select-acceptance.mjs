/**
 * 切片选择（2026-10-21 阶段）headless 页面验收。
 *
 * 真实前端（:3107 turbopack dev 的 /studio 页面代码）+ 路由层 mock（page.route 拦截
 * /video-api/**）：数据是合成的三卡台账，但「两路缩略图用真实项目 b677b 抽出的真实
 * 第一帧 JPG」，播放用可解码的 WebM。登录态属用户（W07 身份链），此脚本只验证
 * 页面行为本身：恢复/选择/一致性/失败回滚，截图与请求记录即为证据。
 *
 * 运行：node __clip-select-acceptance.mjs（工作目录 frontend/）
 */
import { chromium } from "/domain/supoclip-medical-workbench/frontend/node_modules/.pnpm/playwright@1.59.1/node_modules/playwright/index.mjs";
import { readFileSync } from "node:fs";

const PID = "timeline_b677b1639a1c";
const TASK = "autoedit_aaaabbbbcccc";
const ASSETS = "/tmp/clip-select-acceptance";

const CLIPS = [
  { id: "clip-d6a7789e82", name: "剪辑成片 01", topic: "免疫治疗答疑", dur: 8,
    seg: "第一问：免疫治疗最害怕的是什么？" },
  { id: "clip-0af9a07835", name: "剪辑成片 02", topic: "血压管理", dur: 10,
    seg: "第二问：血压高要长期吃药吗？" },
  { id: "clip-7c9e51d0aa", name: "剪辑成片 03", topic: "复查节奏", dur: 9,
    seg: "第三问：多久复查一次才合理？" },
];

const state = {
  ledgerActive: "clip-0af9a07835",     // 初始台账记录：上次选中 02（模拟刷新前状态）
  putCalls: [],                         // PUT active-clip 请求记录
  failPut: false,                       // 置 true 时 PUT 一律 500
};

const card = (c) => ({
  clip_id: c.id, name: c.name, title: c.topic, topic: c.topic, keywords: [],
  duration: c.dur, source_start: 0, source_end: c.dur, task_id: TASK,
  render_task_id: TASK, render_revision: 1, status: "ready",
  video_path: `output/${TASK}/${c.id}/clip_final.mp4`, cover: null, task_history: [],
  segments: [{ start: 0, end: 2, text: c.seg }],
});
const cardsPayload = () => ({
  project_id: PID, card_count: CLIPS.length,
  active_clip_id: state.ledgerActive, active_clip_updated_at: "2026-10-21T02:00:00Z",
  cards: CLIPS.map(card),
});

const firstFrameFor = (clipId) => readFileSync(`${ASSETS}/frame-${clipId}.jpg`);
const webmFor = (clipId) => readFileSync(`${ASSETS}/${clipId}.webm`);

const errors = [];
const browser = await chromium.launch({ executablePath: "/opt/ms-playwright/chromium-1246/chrome-linux64/chrome", args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text().slice(0, 200)); });
page.on("pageerror", (e) => errors.push("PAGEERROR: " + e.message.slice(0, 300)));
page.on("response", (r) => { if (r.url().includes("/artifact")) console.log("ARTIFACT RESP", r.status(), r.headers()["accept-ranges"] || "-", decodeURIComponent(r.url()).slice(-70)); });
page.on("requestfailed", (r) => { if (r.url().includes("/artifact")) console.log("ARTIFACT FAILED", r.failure()?.errorText, r.url().slice(-70)); });

await page.route("**/video-api/**", async (route) => {
  const url = new URL(route.request().url());
  const path = url.pathname;
  const method = route.request().method();
  try {
    if (path === `/video-api/studio/projects/${PID}/clips` && method === "GET") {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(cardsPayload()) });
    }
    const frame = path.match(/^\/video-api\/studio\/projects\/[^/]+\/clips\/(clip-[0-9a-f]+)\/first-frame\.jpg$/);
    if (frame && method === "GET") {
      return route.fulfill({ status: 200, contentType: "image/jpeg", body: firstFrameFor(frame[1]) });
    }
    if (path === `/video-api/studio/projects/${PID}/active-clip` && method === "PUT") {
      const body = JSON.parse(route.request().postData() || "{}");
      state.putCalls.push(body.clip_id ?? null);
      if (state.failPut) return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: { code: "E_WRITE_FAILED", message: "台账写入失败" } }) });
      state.ledgerActive = body.clip_id ?? null;
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ project_id: PID, active_clip_id: body.clip_id ?? null, changed: true }) });
    }
    if (path === `/video-api/studio/timeline/${PID}` && method === "GET") {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        id: PID, timeline: { ranges: CLIPS.map((c) => ({ id: c.id, source_start: 0, source_end: c.dur })) }, transcript: [],
      }) });
    }
    if (path.startsWith("/video-api/studio/auto-edit/") && path.endsWith("/artifact")) {
      const clipId = decodeURIComponent(url.searchParams.get("path") || "").split("/")[2] || "";
      if (CLIPS.some((c) => c.id === clipId)) {
        // Accept-Ranges: none：playwright fulfill 返回 200 全量响应，声明 bytes 会让
        // Chromium 媒体栈在 Range 请求下判 SRC_NOT_SUPPORTED（err=4）。
        return route.fulfill({ status: 200, contentType: "video/webm", body: webmFor(clipId), headers: { "Accept-Ranges": "none" } });
      }
    }
    return route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "mock-404" }) });
  } catch (e) {
    console.log("ROUTE HANDLER ERROR:", e && (e.stack || e.message || String(e)).toString().slice(0, 300));
    return route.fulfill({ status: 500, body: String(e) });
  }
});

const shot = (name) => page.screenshot({ path: `/domain/supoclip-medical-workbench/docs/screenshots/${name}`, fullPage: false });
const activeClipIds = () => page.$$eval(".asset-card.rendered-card.active", (ns) => ns.map((n) => n.getAttribute("data-testid") || n.textContent.slice(0, 12)));
const thumbSrcs = () => page.$$eval(".rendered-cover img", (ns) => ns.map((n) => n.getAttribute("src")));
const playerSrc = () => page.$eval(".player-stage video", (v) => v.getAttribute("src")).catch(() => null);
const playerState = () => page.evaluate(() => {
  const v = document.querySelector(".player-stage video");
  return v ? { rs: v.readyState, err: v.error && v.error.code, msg: v.error && v.error.message, w: v.videoWidth } : null;
}).catch(() => null);
const transcriptText = () => page.$eval(".transcript-text", (n) => n.textContent).catch(() => "");
const waitFor = (ms) => page.waitForTimeout(ms);

const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: Boolean(ok), detail: String(detail).slice(0, 160) }); console.log(`${ok ? "PASS" : "FAIL"} ${name} ${ok ? "" : "| " + detail}`); };

// ---- Stage 1：打开项目 → 恢复上次选中（02），不回写 ----
await page.goto(`http://127.0.0.1:3107/studio?source_id=${PID}`, { waitUntil: "domcontentloaded", timeout: 90000 });
await page.waitForSelector(".asset-card.rendered-card", { timeout: 90000 });
await waitFor(2500);
check("S1 三张素材卡渲染", (await page.$$(".asset-card.rendered-card")).length === 3);
check("S1 恢复上次选中(02)且唯一", (await activeClipIds()).length === 1 && (await page.textContent(".asset-card.rendered-card.active")).includes("剪辑成片 02"));
check("S1 恢复不回写 PUT", state.putCalls.length === 0, JSON.stringify(state.putCalls));
const src1 = await playerSrc();
check("S1 播放器指向 02", (src1 || "").includes("clip-0af9a07835"), src1 || "null");
check("S1 转写与切片一致(02)", (await transcriptText()).includes("第二问"), await transcriptText());
const srcs = await thumbSrcs();
check("S1 三卡缩略图均为 first-frame.jpg", srcs.length === 3 && srcs.every((s) => /first-frame\.jpg$/.test(s || "")), JSON.stringify(srcs));
const loaded = await page.$$eval(".rendered-cover img", (ns) => ns.map((n) => n.naturalWidth));
check("S1 真实第一帧已解码(naturalWidth>0)", loaded.length === 3 && loaded.every((w) => w > 0), JSON.stringify(loaded));
check("S1 缩略图不是生成封面", srcs.every((s) => !(s || "").includes("cover.jpg")), JSON.stringify(srcs));
console.log("S1 video state:", JSON.stringify(await playerState()));
await shot("clip-select-1-restore.png");

// ---- Stage 2：点击 03 → 唯一切换（播放器+转写+选中态），PUT 持久化 ----
await page.$$eval(".asset-card.rendered-card", (ns) => ns.find((n) => n.textContent.includes("剪辑成片 03")).click());
await waitFor(1200);
check("S2 点击03后唯一选中03", (await activeClipIds()).length === 1 && (await page.textContent(".asset-card.rendered-card.active")).includes("剪辑成片 03"));
check("S2 PUT 记录 clip-7c9e51d0aa", JSON.stringify(state.putCalls) === JSON.stringify(["clip-7c9e51d0aa"]), JSON.stringify(state.putCalls));
const src2 = await playerSrc();
check("S2 播放器切换到03", (src2 || "").includes("clip-7c9e51d0aa"), src2 || "null");
check("S2 转写与切片一致(03)", (await transcriptText()).includes("第三问"), await transcriptText());
check("S2 转写不再含02内容", !(await page.$$eval(".transcript-text", (ns) => ns.map((n) => n.textContent).join("|"))).includes("第二问"));
await shot("clip-select-2-click-03.png");

// ---- Stage 3：刷新 → 恢复 03（服务端已记录 C）----
await page.reload({ waitUntil: "domcontentloaded" });
await page.waitForSelector(".asset-card.rendered-card", { timeout: 90000 });
await waitFor(2500);
const putsBeforeReload = state.putCalls.length;
check("S3 刷新后恢复03且唯一", (await activeClipIds()).length === 1 && (await page.textContent(".asset-card.rendered-card.active")).includes("剪辑成片 03"));
check("S3 恢复通道不回写", state.putCalls.length === putsBeforeReload);
check("S3 播放器恢复03", ((await playerSrc()) || "").includes("clip-7c9e51d0aa"));
check("S3 转写恢复03", (await transcriptText()).includes("第三问"));
await shot("clip-select-3-refresh-restore.png");

// ---- Stage 4：切换失败 → 保留原切片（03）----
state.failPut = true;
await page.$$eval(".asset-card.rendered-card", (ns) => ns.find((n) => n.textContent.includes("剪辑成片 01")).click());
await waitFor(1800);
check("S4 失败回滚：唯一选中仍是03", (await activeClipIds()).length === 1 && (await page.textContent(".asset-card.rendered-card.active")).includes("剪辑成片 03"));
check("S4 失败 PUT 已如实记录", state.putCalls[state.putCalls.length - 1] === "clip-d6a7789e82", JSON.stringify(state.putCalls));
check("S4 播放器保留03", ((await playerSrc()) || "").includes("clip-7c9e51d0aa"));
check("S4 转写保留03", (await transcriptText()).includes("第三问"));
await shot("clip-select-4-rollback.png");

// ---- 汇总 ----
// 404 = mock 对次要端点的兜底；500 = Stage4 刻意注入的 PUT 失败（浏览器按资源错误记录，属预期）。
const benign = (e) => /video-api|Failed to fetch|favicon|404|mock-404|401|studio_auth|500 \(Internal Server Error\)/.test(e);
const hardErrors = errors.filter((e) => !benign(e));
check("无新增页面代码错误", hardErrors.length === 0, JSON.stringify(hardErrors.slice(0, 4)));
console.log("SUMMARY:", JSON.stringify({ results, putCalls: state.putCalls, hardErrors }));
await browser.close();
process.exit(results.every((r) => r.ok) ? 0 : 1);
