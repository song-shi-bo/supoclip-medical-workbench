import { redirect } from "next/navigation";

export default function RootPage() {
  // 工作台唯一入口：根路径直接跳转 Studio（legacy 首页/任务页已随 A 线管线移除）
  redirect("/studio");
}
