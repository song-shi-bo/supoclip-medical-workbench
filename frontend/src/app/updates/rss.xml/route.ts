/**
 * 更新日志 RSS：与页面同一份公开数据源生成，只包含已完成条目。
 * 订阅入口在 /updates 页顶部；RSS 阅读器拉取 /updates/rss.xml。
 */
import publicUpdatesJson from "../../../../data/updates.public.json";
import type { UpdateEntry } from "@/lib/updates";

const publicUpdates = publicUpdatesJson as unknown as UpdateEntry[];

function escapeXml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&apos;");
}

export async function GET() {
  const entries = publicUpdates
    .filter((entry) => entry.status === "done")
    .sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0))
    .slice(0, 30);

  const items = entries
    .map((entry) => {
      const description = [
        entry.summary,
        ...(entry.compare ? ["旧版/新版对比见更新页。"] : []),
        ...(entry.entry ? [`入口：${entry.entry}`] : []),
      ].join("　");
      return [
        "    <item>",
        `      <title>${escapeXml(entry.title)}</title>`,
        `      <link>https://supoclip.com/updates</link>`,
        `      <guid isPermaLink="false">supoclip-update-${escapeXml(entry.id)}</guid>`,
        `      <pubDate>${new Date(`${entry.date}T09:00:00Z`).toUTCString()}</pubDate>`,
        `      <description>${escapeXml(description)}</description>`,
        "    </item>",
      ].join("\n");
    })
    .join("\n");

  const xml = [
    `<?xml version="1.0" encoding="UTF-8"?>`,
    `<rss version="2.0">`,
    "  <channel>",
    "    <title>SupoClip 更新日志</title>",
    "    <link>https://supoclip.com/updates</link>",
    "    <description>SupoClip 工作台的用户可感知更新</description>",
    "    <language>zh-cn</language>",
    items,
    "  </channel>",
    "</rss>",
  ].join("\n");

  return new Response(xml, {
    headers: {
      "content-type": "application/rss+xml; charset=utf-8",
      "cache-control": "public, max-age=3600",
    },
  });
}
