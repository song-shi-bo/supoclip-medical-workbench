"""本地 OpenAI 兼容 Mock 模型服务 —— 仅用于验证剪辑助手 LLM 接入链路。

按 user_message 关键字返回与真实模型同构的 chat.completion 响应，
内容符合 studio_copilot._LlmCopilotTurn 协议（reply/actions/requires_confirmation）。
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

CASES = [
    ("跳到 1 分 20 秒并播放|跳到 01:20", {
        "reply": "我已找到 01:20 附近的内容，已定位并开始播放。",
        "actions": [
            {"tool": "jump_to_time", "arguments": {"seconds": 80}, "execution": "run", "label": "跳到 01:20"},
            {"tool": "play", "arguments": {}, "execution": "run", "label": "播放当前视频"},
        ],
        "requires_confirmation": False,
    }),
    ("金句|关键词", {
        "reply": "正在为当前视频提取金句和关键词。",
        "actions": [{"tool": "extract_insights", "arguments": {}, "execution": "run", "label": "提取金句和关键词"}],
        "requires_confirmation": False,
    }),
    ("30 秒|规划", {
        "reply": "结合金句候选，我规划了一条约 30 秒的短视频结构：先钩子后重点，已逐段高亮，确认后再开始自动剪辑。",
        "actions": [
            {"tool": "highlight_time_range", "arguments": {"start": 9, "end": 13}, "execution": "run", "label": "定位开头钩子段"},
            {"tool": "highlight_time_range", "arguments": {"start": 22, "end": 36}, "execution": "run", "label": "定位核心讲解段"},
        ],
        "requires_confirmation": False,
    }),
    ("开始自动剪辑", {
        "reply": "将按固定剪辑流程开始自动剪辑，请在界面确认后执行。",
        "actions": [{"tool": "start_auto_edit", "arguments": {}, "execution": "confirm", "label": "开始自动剪辑", "requires_confirmation": True}],
        "requires_confirmation": True,
    }),
    ("导出", {
        "reply": "将导出当前成片，请确认。",
        "actions": [{"tool": "export_active_clip", "arguments": {}, "execution": "confirm", "label": "导出当前成片", "requires_confirmation": True}],
        "requires_confirmation": True,
    }),
]
FALLBACK = {"reply": "已收到指令（mock）。", "actions": [], "requires_confirmation": False}


def pick(message: str) -> dict:
    for pattern, plan in CASES:
        for keyword in pattern.split("|"):
            if keyword in message:
                return plan
    return FALLBACK


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        message = ""
        for msg in reversed(body.get("messages", [])):
            if msg.get("role") == "user":
                message = str(msg.get("content", ""))
                break
        plan = pick(message)
        content = json.dumps(plan, ensure_ascii=False)
        response = {
            "id": "chatcmpl-mock", "object": "chat.completion", "created": 0,
            "model": body.get("model", "mock"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        data = json.dumps(response, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 9123), Handler).serve_forever()
