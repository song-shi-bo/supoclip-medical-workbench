"use client";

// 播放器标题栏（卡片第一层，≈56px）：
//  左：固定文案「播放器 - 时间线01」（窄窗不换行，超长省略号）；
//  右：**只放一个**三横线按钮，展开播放器菜单（查看选项，不影响导出）。
//
// 语义红线：菜单里全部是「观看」操作 —— 适合画布（复位 viewerZoom）、网格参考线、
// 循环播放；没有任何一项会写入 ClipTransform 或进入导出参数。

import { useEffect, useRef, useState } from "react";
import { Maximize2, Menu, Settings2 } from "lucide-react";

export const DEFAULT_PLAYER_TITLE = "播放器 - 时间线01";

type PlayerTitleBarProps = {
  // 固定文案；超长时由 CSS 省略号处理（不换行）。
  title?: string;
  guides: boolean;
  loop: boolean;
  // 复位查看比例（viewerZoom）。只影响预览，不影响导出。
  onFitView: () => void;
  onToggleGuides: () => void;
  onToggleLoop: () => void;
};

export function PlayerTitleBar({
  title = DEFAULT_PLAYER_TITLE,
  guides,
  loop,
  onFitView,
  onToggleGuides,
  onToggleLoop,
}: PlayerTitleBarProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // 点击外部关闭播放器菜单。
  useEffect(() => {
    if (!menuOpen) return undefined;
    const onPointerDown = (event: MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [menuOpen]);

  return (
    <header className="player-title-bar">
      <strong className="player-title" title={title}>{title}</strong>
      <div className="player-title-more" ref={menuRef}>
        <button
          type="button"
          className={`player-icon-button${menuOpen ? " active" : ""}`}
          aria-label="播放器菜单"
          aria-expanded={menuOpen}
          title="播放器菜单：适合画布、网格参考线、循环播放（都只影响预览，不影响导出）"
          onClick={() => setMenuOpen((value) => !value)}
        >
          <Menu size={16} />
        </button>
        {menuOpen && (
          <div className="player-popover player-title-menu" role="menu" aria-label="播放器菜单">
            <p>查看选项（不影响导出）</p>
            <button
              type="button"
              role="menuitem"
              title="适合画布：复位查看比例（viewerZoom），让整个画布完整显示在播放器里。仅调整查看比例，不影响导出。"
              onClick={onFitView}
            >
              <Maximize2 size={14} />适合画布
            </button>
            <button
              type="button"
              role="menuitemcheckbox"
              aria-checked={guides}
              title="显示三分网格参考线，仅用于观察构图"
              onClick={onToggleGuides}
            >
              <Settings2 size={14} />网格参考线<em>{guides ? "开" : "关"}</em>
            </button>
            <button
              type="button"
              role="menuitemcheckbox"
              aria-checked={loop}
              title="播放到结尾后自动从头循环，仅影响本地预览"
              onClick={onToggleLoop}
            >
              <Settings2 size={14} />循环播放<em>{loop ? "开" : "关"}</em>
            </button>
          </div>
        )}
      </div>
    </header>
  );
}
