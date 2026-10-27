"use client";

/**
 * 剪辑助手（Agent）独立左栏。
 * 只负责展示对话与确认按钮；所有动作由工作台页面的白名单执行器落地。
 */

import { useEffect, useRef, useState } from "react";
import { Bot, ChevronLeft, ChevronRight, LoaderCircle, Send, User, Wrench } from "lucide-react";

export type CopilotAction = {
  id: string;
  type: string;
  label: string;
  execution: "run" | "confirm";
  start?: number | null;
  end?: number | null;
  index?: number | null;
  task_id?: string | null;
  clip_id?: string | null;
  speaker?: string | null;
  // 封面动作字段（make_cover / adopt_cover）：
  // style_id = 「按风格重新生成」用的风格；cover_source_clip_id = 「沿用已生成封面」的来源成片；
  // replace_intro = 是否同时替换 0.8s 片头。
  style_id?: string | null;
  cover_source_clip_id?: string | null;
  replace_intro?: boolean | null;
  // 类型化局部任务字段（update_title / update_subtitle_style / update_timeline / update_bgm）：
  // task_type = 后端动作到任务类型的映射；instruction = 用户原话；remove_ranges = 删除区间。
  task_type?: string | null;
  instruction?: string | null;
  title?: string | null;
  remove_ranges?: number[][] | null;
  font_scale?: number | null;
  // BGM 调整（update_bgm）：音量 0..1 / 淡入淡出秒 / 移除全部 BGM 轨。
  bgm_volume?: number | null;
  bgm_fade_in_s?: number | null;
  bgm_fade_out_s?: number | null;
  bgm_remove?: boolean | null;
  note?: string | null;
  status?: "run" | "confirm" | "executed" | "failed" | "dismissed";
  result?: string;
};

export type CopilotMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
  engine?: "llm" | "rules" | null;
  notice?: string | null;
  actions?: CopilotAction[];
};

const ACTION_STATUS_TEXT: Record<string, string> = {
  executed: "已执行",
  failed: "执行失败",
  dismissed: "已忽略",
  confirm: "等待确认",
  run: "执行中",
};

export function StudioCopilot({
  messages,
  busy,
  collapsed,
  onToggle,
  onSend,
  onRunAction,
  onDismissAction,
}: {
  messages: CopilotMessage[];
  busy: boolean;
  collapsed: boolean;
  onToggle: () => void;
  onSend: (text: string) => void;
  onRunAction: (action: CopilotAction) => void;
  onDismissAction: (action: CopilotAction) => void;
}) {
  const [draft, setDraft] = useState("");
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const list = listRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [messages]);

  const submit = () => {
    const text = draft.trim();
    if (!text || busy) return;
    setDraft("");
    onSend(text);
  };

  if (collapsed) {
    return (
      <aside className="copilot-pane collapsed" aria-label="剪辑助手（已收起）">
        <button className="copilot-expand" onClick={onToggle} aria-label="展开剪辑助手" title="展开剪辑助手">
          <ChevronRight size={16} />
          <Bot size={18} />
        </button>
      </aside>
    );
  }

  return (
    <aside className="copilot-pane" aria-label="剪辑助手">
      <div className="pane-title">
        <strong><Bot size={16} /> 剪辑助手</strong>
        <button className="copilot-collapse" onClick={onToggle} aria-label="收起剪辑助手" title="收起">
          <ChevronLeft size={15} />
        </button>
      </div>
      <div className="copilot-messages" ref={listRef}>
        {messages.length === 0 && (
          <div className="copilot-empty">
            <Bot size={22} />
            <p>我是剪辑助手，可以直接控制当前工作区。试试：</p>
            <ul>
              <li>跳到 1 分 20 秒并播放</li>
              <li>打开第 1 个成片</li>
              <li>提取金句和关键词</li>
              <li>开始自动剪辑</li>
            </ul>
          </div>
        )}
        {messages.map((message) => (
          <article key={message.id} className={`copilot-msg ${message.role}`}>
            <span className="copilot-avatar">{message.role === "user" ? <User size={13} /> : <Bot size={13} />}</span>
            <div className="copilot-bubble">
              <p>{message.text}</p>
              {message.engine && message.role === "assistant" && (
                <small className="copilot-engine">{message.engine === "llm" ? "模型助手" : "本机规则助手"}</small>
              )}
              {message.notice && <small className="copilot-notice">{message.notice}</small>}
              {message.actions && message.actions.length > 0 && (
                <div className="copilot-actions">
                  {message.actions.map((action) => {
                    const resolved = action.status === "executed" || action.status === "failed" || action.status === "dismissed";
                    return (
                      <div key={action.id} className={`copilot-action ${action.status || action.execution}`}>
                        <span className="copilot-action-label"><Wrench size={12} />{action.label}</span>
                        {action.status === "executed" && <em className="ok">✓ {action.result || "已执行"}</em>}
                        {action.status === "failed" && <em className="bad">✕ {action.result || "执行失败"}</em>}
                        {action.status === "dismissed" && <em>已忽略</em>}
                        {action.status === "confirm" && (
                          <span className="copilot-confirm-row">
                            <button className="confirm" onClick={() => onRunAction(action)}>确认执行</button>
                            <button className="dismiss" onClick={() => onDismissAction(action)}>忽略</button>
                          </span>
                        )}
                        {!resolved && action.status !== "confirm" && <em className="pending">{ACTION_STATUS_TEXT[action.status || action.execution]}</em>}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </article>
        ))}
        {busy && <div className="copilot-msg assistant"><span className="copilot-avatar"><Bot size={13} /></span><div className="copilot-bubble"><p className="copilot-thinking"><LoaderCircle size={13} /> 正在思考…</p></div></div>}
      </div>
      <div className="copilot-input">
        <input
          value={draft}
          placeholder="输入指令，如：跳到 01:20 并播放"
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Enter" && !event.nativeEvent.isComposing) submit(); }}
          disabled={busy}
        />
        <button onClick={submit} disabled={busy || !draft.trim()} aria-label="发送">{busy ? <LoaderCircle size={16} /> : <Send size={16} />}</button>
      </div>
    </aside>
  );
}
