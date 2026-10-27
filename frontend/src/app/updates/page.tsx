import { Suspense } from "react";
import type { Metadata } from "next";
import publicUpdatesJson from "../../../data/updates.public.json";
import type { UpdateEntry } from "@/lib/updates";
import { UpdatesClient } from "./updates-client";

export const metadata: Metadata = {
  title: "更新日志",
  description: "SupoClip 工作台的用户可感知更新与开发日志。",
  alternates: {
    types: { "application/rss+xml": "/updates/rss.xml" },
  },
};

const publicUpdates = publicUpdatesJson as unknown as UpdateEntry[];

export default function UpdatesPage() {
  return (
    <main className="mx-auto w-full max-w-3xl px-4 pb-16 pt-10 sm:px-6">
      <Suspense
        fallback={<p className="text-sm text-muted-foreground">加载中…</p>}
      >
        <UpdatesClient publicEntries={publicUpdates} />
      </Suspense>
    </main>
  );
}
