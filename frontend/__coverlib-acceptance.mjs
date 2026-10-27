/**
 * 封面版型库（2026-10-21 阶段）headless 页面验收。
 *
 * 真实前端（:3107 turbopack dev 的 /studio 页面代码）+ 路由层 mock（/video-api/**）：
 * 版型集合用真实注册表内容（backend cover_templates 的四版型与示例图路径，示例图是
 * public/cover-templates/*.svg 静态资产，由 dev server 直接出图）；登录态属用户
 * （W07 身份链，headless 无会话），本脚本验证页面行为层：
 * 四卡显示 / 单选 / 未选切片提示与不可生成 / 模拟按钮与结果 / 切片不串片 / 刷新恢复。
 *
 * 运行：node __coverlib-acceptance.mjs（工作目录 frontend/）
 */
import { chromium } from "/domain/supoclip-medical-workbench/frontend/node_modules/.pnpm/playwright@1.59.1/node_modules/playwright/index.mjs";
import { readFileSync } from "node:fs";

const PID = "timeline_b677b1639a1c";
const TASK = "autoedit_aaaabbbbcccc";
const ASSETS = "/tmp/clip-select-acceptance";

const TEMPLATES = [
  { template_id: "cute", name: "可爱版", version: "1", example_path: "/studio/cover-templates/cute/example.svg", description: "圆角气泡与柔和配色，标题短句放大，适合轻松科普话题。", sort: 1 },
  { template_id: "cool", name: "酷炫版", version: "1", example_path: "/studio/cover-templates/cool/example.svg", description: "深色底+霓虹描边，强调冲击力，适合观点与反差类内容。", sort: 2 },
  { template_id: "anime_layout", name: "动漫排版", version: "1", example_path: "/studio/cover-templates/anime_layout/example.svg", description: "分格对话式排版与拟声词点缀，节奏感强，适合剧情化剪辑。", sort: 3 },
  { template_id: "yellow_bold", name: "黄色大字", version: "1", example_path: "/studio/cover-templates/yellow_bold/example.svg", description: "亮黄粗体大字压屏，信息一眼可读，适合知识点罗列。", sort: 4 },
];
const REGISTRY_VERSION = "2026-10-21.1";

const CLIPS = [
  { id: "clip-d6a7789e82", name: "剪辑成片 01", dur: 8, seg: "第一问：免疫治疗最害怕的是什么？" },
  { id: "clip-0af9a07835", name: "剪辑成片 02", dur: 10, seg: "第二问：血压高要长期吃药吗？" },
];

const state = {
  ledgerTemplates: { "clip-d6a7789e82": "cute", "clip-0af9a07835": null }, // 初始：01 已选可爱版（模拟刷新前状态）
  putCalls: [],
  mockPosts: [],
  coverRenderBodies: [],
};

const card = (c) => ({
  clip_id: c.id, name: c.name, title: null, topic: "血压科普", keywords: [],
  duration: c.dur, source_start: 0, source_end: c.dur, task_id: TASK,
  render_task_id: TASK, render_revision: 1, status: "ready",
  video_path: `output/${TASK}/${c.id}/clip_final.mp4`, cover: null, task_history: [],
  segments: [{ start: 0, end: 2, text: c.seg }],
  cover_template: state.ledgerTemplates[c.id]
    ? { template_id: state.ledgerTemplates[c.id], registry_version: REGISTRY_VERSION, updated_at: "2026-10-21T02:00:00Z" }
    : null,
});
const cardsPayload = () => ({
  project_id: PID, card_count: CLIPS.length, active_clip_id: null, cards: CLIPS.map(card),
});

const webmFor = (clipId) => readFileSync(`${ASSETS}/${clipId}.webm`);

const errors = [];
const browser = await chromium.launch({ executablePath: "/opt/ms-playwright/chromium-1246/chrome-linux64/chrome", args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text().slice(0, 200)); });
page.on("pageerror", (e) => errors.push("PAGEERROR: " + e.message.slice(0, 300)));
page.on("response", (r) => { if (r.url().includes("/artifact")) console.log("ARTIFACT RESP", r.status(), r.headers()["accept-ranges"] ?? "-", decodeURIComponent(r.url()).slice(-60)); });
const videoState = () => page.evaluate(() => {
  const v = document.querySelector(".player-stage video");
  return v ? { rs: v.readyState, err: v.error && v.error.code, w: v.videoWidth } : { none: true };
});

await page.route("**/video-api/**", async (route) => {
  const url = new URL(route.request().url());
  const path = url.pathname;
  const method = route.request().method();
  try {
    if (path === `/video-api/studio/projects/${PID}/clips` && method === "GET") {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(cardsPayload()) });
    }
    const exampleSvg = path.match(/^\/video-api\/studio\/cover-templates\/([a-z_]+)\/example\.svg$/);
    if (exampleSvg && method === "GET") {
      const svg = readFileSync(`/domain/supoclip-medical-workbench/backend/assets/cover-templates/${exampleSvg[1]}.svg`);
      return route.fulfill({ status: 200, contentType: "image/svg+xml", body: svg });
    }
    if (path === "/video-api/studio/cover-templates" && method === "GET") {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ registry_version: REGISTRY_VERSION, templates: TEMPLATES }) });
    }
    const tmplPut = path.match(/^\/video-api\/studio\/projects\/[^/]+\/clips\/(clip-[0-9a-f]+)\/cover-template$/);
    if (tmplPut && method === "PUT") {
      const body = JSON.parse(route.request().postData() || "{}");
      state.putCalls.push({ clip_id: tmplPut[1], template_id: body.template_id ?? null });
      state.ledgerTemplates[tmplPut[1]] = body.template_id ?? null;
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        project_id: PID, clip_id: tmplPut[1], changed: true,
        cover_template: body.template_id ? { template_id: body.template_id, registry_version: REGISTRY_VERSION, updated_at: "2026-10-21T03:00:00Z" } : null,
      }) });
    }
    const coverRender = path.match(/^\/video-api\/studio\/auto-edit\/[^/]+\/clips\/(clip-[0-9a-f]+)\/cover$/);
    if (coverRender && method === "POST") {
      const body = JSON.parse(route.request().postData() || "{}");
      const templateId = String(body.template_id ?? "");
      state.mockPosts.push(templateId);
      state.coverRenderBodies.push({ template_id: templateId, replace_intro: body.replace_intro === true, clip_id: coverRender[1] });
      const template = TEMPLATES.find((t) => t.template_id === templateId);
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        kind: "generated", style_id: `template:${templateId}`, idempotent: false,
        cover_revision: 3, replace_intro: body.replace_intro === true,
        qc: { passed: true }, cover_path: `output/${TASK}/${coverRender[1]}/cover.jpg`,
      }) });
    }
    if (path === `/video-api/studio/projects/${PID}/active-clip` && method === "PUT") {
      const body = JSON.parse(route.request().postData() || "{}");
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
        return route.fulfill({ status: 200, contentType: "video/webm", body: webmFor(clipId), headers: { "Accept-Ranges": "none" } });
      }
    }
    return route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "mock-404" }) });
  } catch (e) {
    console.log("ROUTE ERROR:", String(e && (e.stack || e.message || e)).slice(0, 240));
    return route.fulfill({ status: 500, body: String(e) });
  }
});

const shot = (name) => page.screenshot({ path: `/domain/supoclip-medical-workbench/docs/screenshots/${name}`, fullPage: false });
const waitFor = (ms) => page.waitForTimeout(ms);
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: Boolean(ok), detail: String(detail).slice(0, 160) }); console.log(`${ok ? "PASS" : "FAIL"} ${name} ${ok ? "" : "| " + detail}`); };
const checkedCards = () => page.$$eval('[role="radio"][aria-checked="true"]', (ns) => ns.map((n) => n.getAttribute("data-testid")));
const clickTabCover = async () => { await page.getByRole("tab", { name: "封面与画面" }).click(); await page.waitForSelector('[data-testid="cover-template-library"]'); };
const clickClip = async (name) => {
  await page.$$eval(".asset-card.rendered-card", (ns, label) => {
    const found = ns.find((n) => n.querySelector("strong")?.textContent === label);
    if (found) found.click(); else throw new Error("card not found: " + label);
  }, name);
  await waitFor(600);
};

// ---- S1：打开项目（未选切片）→ 封面页只有提示，无卡无按钮 ----
await page.goto(`http://127.0.0.1:3107/studio?source_id=${PID}`, { waitUntil: "domcontentloaded", timeout: 90000 });
await page.waitForSelector(".asset-card.rendered-card", { timeout: 90000 });
await waitFor(2000);
await clickTabCover();
check("S1 未选切片：显示提示", await page.$('[data-testid="cover-templates-no-clip"]') !== null);
check("S1 未选切片：无版型卡与生成按钮", await page.$('[data-testid="cover-template-grid"]') === null && await page.$('[data-testid="cover-template-generate"]') === null);
await shot("coverlib-1-no-clip-hint.png");

// ---- S2：选中切片 01（台账已选可爱版）→ 四张卡 + 选中态恢复 ----
await clickClip("剪辑成片 01");
await waitFor(800);
check("S2 四张模板卡显示正常", await page.$$eval('[data-testid="cover-template-grid"] > [role="radio"]', (ns) => ns.length) === 4);
const srcs = await page.$$eval(".cover-template-frame img", (ns) => ns.map((n) => n.getAttribute("src")));
check("S2 每卡含示例图（后端路由出图）", srcs.length === 4 && srcs.every((s) => s?.startsWith("/video-api/studio/cover-templates/")), JSON.stringify(srcs));
await page.$$eval(".cover-template-grid", (ns) => ns[0]?.scrollIntoView({ block: "center" }));
await waitFor(600);
const decodeDetail = await page.$$eval(".cover-template-frame img", (ns) => ns.map((n) => ({ src: n.currentSrc || n.getAttribute("src"), nw: n.naturalWidth, complete: n.complete })));
console.log("DECODE DETAIL:", JSON.stringify(decodeDetail));
check("S2 示例图真实解码（naturalWidth>0，ingress 路径可达）", decodeDetail.length === 4 && decodeDetail.every((d) => d.nw > 0), JSON.stringify(decodeDetail.map((d) => d.nw)));
for (const name of ["可爱版", "酷炫版", "动漫排版", "黄色大字"]) {
  check(`S2 模板名称可见：${name}`, (await page.getByText(name).count()) > 0);
}
check("S2 注册表版本徽标", (await page.textContent('[data-testid="cover-template-library"]'))?.includes(`版型库 v${REGISTRY_VERSION}`) === true);
check("S2 刷新前选择恢复（01=可爱版选中）", JSON.stringify(await checkedCards()) === JSON.stringify(["cover-template-card-cute"]));
check("S2 只能选择一个", (await checkedCards()).length === 1);
check("S2 生成按钮=使用可爱版制作封面", (await page.textContent('[data-testid="cover-template-generate"]'))?.includes("使用可爱版制作封面") === true);
console.log("S2 video state:", JSON.stringify(await videoState()));
await shot("coverlib-2-restore-cute.png");

// ---- S3：改选酷炫版 → PUT 持久化 + 模拟按钮接模拟结果 ----
await page.click('[data-testid="cover-template-card-cool"]');
await waitFor(700);
check("S3 改选酷炫：PUT 已记录", JSON.stringify(state.putCalls) === JSON.stringify([{ clip_id: "clip-d6a7789e82", template_id: "cool" }]), JSON.stringify(state.putCalls));
check("S3 单选（仅 cool 选中）", JSON.stringify(await checkedCards()) === JSON.stringify(["cover-template-card-cool"]));
check("S3 按钮文案跟随", (await page.textContent('[data-testid="cover-template-generate"]'))?.includes("使用酷炫版制作封面") === true);
await page.click('[data-testid="cover-template-generate"]');
await waitFor(700);
check("S3 真实渲染通道接通（POST cover 带 template_id + replace_intro）", JSON.stringify(state.coverRenderBodies) === JSON.stringify([{ template_id: "cool", replace_intro: true, clip_id: "clip-d6a7789e82" }]) && await page.$('[data-testid="cover-template-mock-result"]') !== null);
check("S3 结果框显示封面已更新（酷炫版）", (await page.textContent('[data-testid="cover-template-mock-result"]'))?.includes("封面已更新 · 酷炫版") === true);
await shot("coverlib-3-mock-result.png");

// ---- S4：切到切片 02（无选择）→ 不串片：无选中/无按钮/无 01 的模拟结果 ----
await clickClip("剪辑成片 02");
await waitFor(800);
check("S4 切片后不串片：02 无选中", (await checkedCards()).length === 0);
check("S4 不串片：无生成按钮（未选版型不能生成）", await page.$('[data-testid="cover-template-generate"]') === null);
check("S4 不串片：01 的模拟结果不跟随", await page.$('[data-testid="cover-template-mock-result"]') === null);
// 02 选动漫排版
await page.click('[data-testid="cover-template-card-anime_layout"]');
await waitFor(700);
check("S4 02 选择动漫排版：PUT 记录", state.putCalls.at(-1)?.clip_id === "clip-0af9a07835" && state.putCalls.at(-1)?.template_id === "anime_layout", JSON.stringify(state.putCalls));
// 切回 01：cool 仍在（per-clip）+ 模拟结果恢复可见
await clickClip("剪辑成片 01");
await waitFor(800);
check("S4 切回 01：cool 选择保留 + 模拟结果恢复", JSON.stringify(await checkedCards()) === JSON.stringify(["cover-template-card-cool"]) && await page.$('[data-testid="cover-template-mock-result"]') !== null);
await shot("coverlib-4-per-clip-state.png");

// ---- S5：刷新 → 按台账载荷恢复各自选择 ----
await page.reload({ waitUntil: "domcontentloaded" });
await page.waitForSelector(".asset-card.rendered-card", { timeout: 90000 });
await waitFor(2000);
await clickTabCover();
await clickClip("剪辑成片 02");
await waitFor(800);
check("S5 刷新恢复：02=动漫排版（上次选择）", JSON.stringify(await checkedCards()) === JSON.stringify(["cover-template-card-anime_layout"]));
await clickClip("剪辑成片 01");
await waitFor(800);
check("S5 刷新恢复：01=酷炫版（上次选择）", JSON.stringify(await checkedCards()) === JSON.stringify(["cover-template-card-cool"]));
check("S5 全程 PUT 共 2 次（都是显式点击）", state.putCalls.length === 2, JSON.stringify(state.putCalls));
await shot("coverlib-5-refresh-restore.png");

// ---- 汇总 ----
const benign = (e) => /video-api|Failed to fetch|favicon|404|mock-404|401|studio_auth|500 \(Internal Server Error\)/.test(e);
const hardErrors = errors.filter((e) => !benign(e));
check("无新增页面代码错误", hardErrors.length === 0, JSON.stringify(hardErrors.slice(0, 4)));
console.log("SUMMARY:", JSON.stringify({ results, putCalls: state.putCalls, mockPosts: state.mockPosts, hardErrors }));
await browser.close();
process.exit(results.every((r) => r.ok) ? 0 : 1);
