"""助手「意图 → 任务类型」路由测试（纯规则引擎，不调模型、不连数据库）。

任务书 §四 要求：助手的每一次「改这条成片」都必须落到**具体局部任务类型**，
并且已经有成片时绝不再启动 AUTO_EDIT（那会多出一张重复卡片）。
"""

from __future__ import annotations

import pytest

from src.studio_copilot import (
    ACTION_TASK_TYPE,
    CONFIRM_ACTION_TYPES,
    CONFIRM_HINTS,
    TASK_TYPE_REGENERATE_COVER,
    TASK_TYPE_UPDATE_AUDIO,
    TASK_TYPE_UPDATE_SUBTITLE_STYLE,
    TASK_TYPE_UPDATE_TIMELINE,
    TASK_TYPE_UPDATE_TITLE,
    CopilotTurn,
    _rule_based_copilot,
)

CLIPS = [
    {"task_id": "autoedit_aaaabbbbcccc", "clip_id": "clip-01", "name": "剪辑成片 01",
     "topic": "健康科普", "duration": 16.9},
    {"task_id": "autoedit_aaaabbbbcccc", "clip_id": "clip-02", "name": "剪辑成片 02",
     "topic": "门诊流程", "duration": 12.0},
]


def _turn(message: str, active: str = "clip-01", **kwargs) -> CopilotTurn:
    return CopilotTurn(
        message=message,
        rendered_clips=CLIPS,
        active_clip_id=active,
        active_task_id="autoedit_aaaabbbbcccc",
        asr_segments=[{"start": 0, "end": 5, "text": "示例转写"}],
        duration=30.0,
        **kwargs,
    )


def _first(turn: CopilotTurn):
    result = _rule_based_copilot(turn)
    assert result.actions, f"没有产出动作：{result.reply}"
    return result.actions[0]


# ---------------------------------------------------------------------------
# 新的局部任务动作类型都在白名单与确认提示里
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action_type", sorted(ACTION_TASK_TYPE))
def test_local_task_actions_are_confirm_only(action_type):
    assert action_type in CONFIRM_ACTION_TYPES
    assert action_type in CONFIRM_HINTS


def test_canonical_action_maps_to_canonical_task_type():
    assert ACTION_TASK_TYPE["regenerate_cover"] == TASK_TYPE_REGENERATE_COVER
    assert ACTION_TASK_TYPE["update_title"] == TASK_TYPE_UPDATE_TITLE
    assert ACTION_TASK_TYPE["update_subtitle_style"] == TASK_TYPE_UPDATE_SUBTITLE_STYLE
    assert ACTION_TASK_TYPE["update_timeline"] == TASK_TYPE_UPDATE_TIMELINE
    assert ACTION_TASK_TYPE["update_bgm"] == TASK_TYPE_UPDATE_AUDIO


# ---------------------------------------------------------------------------
# 成员示例逐条映射
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "重新制作封面，主题不超过 12 个字",
    "封面主题太长压缩到 12 字内",
    "重新做一下封面",
])
def test_cover_intents_route_to_regenerate_cover(message):
    action = _first(_turn(message))
    assert action.type == "regenerate_cover"
    assert action.task_type == TASK_TYPE_REGENERATE_COVER
    assert action.clip_id == "clip-01"
    assert action.execution == "confirm"


def test_cover_without_cutout_intent_switches_to_realframe():
    """「封面不要人物抠图」= 明确要第二版真实帧，而不是被「抠图」误判成 cutout_v3。"""
    action = _first(_turn("封面不要人物抠图"))
    assert action.type == "regenerate_cover"
    assert action.style_id == "realframe_v2"


def test_cover_with_cutout_intent_uses_cutout_v3():
    action = _first(_turn("重新制作封面用第三版抠像"))
    assert action.style_id == "cutout_v3"


def test_subtitle_intent_routes_to_style_update():
    action = _first(_turn("字幕放大一点"))
    assert action.type == "update_subtitle_style"
    assert action.task_type == TASK_TYPE_UPDATE_SUBTITLE_STYLE
    assert action.font_scale == 1.15


def test_timeline_intent_routes_to_timeline_update():
    action = _first(_turn("删除第 8 秒到第 10 秒"))
    assert action.type == "update_timeline"
    assert action.task_type == TASK_TYPE_UPDATE_TIMELINE
    assert action.remove_ranges == [[8.0, 10.0]]


def test_title_intent_routes_to_title_update():
    action = _first(_turn("重新生成这一条成片的标题"))
    assert action.type == "update_title"
    assert action.task_type == TASK_TYPE_UPDATE_TITLE


# ---------------------------------------------------------------------------
# BGM 音量/淡入淡出/移除（update_bgm → UPDATE_AUDIO 局部任务）
# ---------------------------------------------------------------------------

def test_bgm_volume_intent_routes_to_audio_update():
    action = _first(_turn("BGM 小声一点"))
    assert action.type == "update_bgm"
    assert action.task_type == TASK_TYPE_UPDATE_AUDIO
    assert action.bgm_volume == 0.6
    assert action.execution == "confirm"
    assert action.clip_id == "clip-01"


def test_bgm_explicit_percent_volume_is_parsed():
    assert _first(_turn("BGM 音量 50%")).bgm_volume == 0.5
    assert _first(_turn("BGM 音量调到 0.8")).bgm_volume == 0.8


def test_bgm_remove_intent_routes_to_audio_update():
    action = _first(_turn("去掉背景音乐"))
    assert action.type == "update_bgm"
    assert action.task_type == TASK_TYPE_UPDATE_AUDIO
    assert action.bgm_remove is True
    assert action.bgm_volume is None


def test_bgm_fade_intent_parses_seconds():
    action = _first(_turn("BGM 淡入 2 秒 淡出 1.5 秒"))
    assert action.type == "update_bgm"
    assert action.bgm_fade_in_s == 2.0
    assert action.bgm_fade_out_s == 1.5


def test_bgm_switch_intent_asks_for_upload_without_action():
    """「换一首 BGM」需要上传文件：只引导去效果页，不发出动作。"""
    result = _rule_based_copilot(_turn("给这条成片换一首 BGM"))
    assert result.actions == []
    assert "效果页" in result.reply


def test_bgm_vague_intent_asks_back_without_action():
    result = _rule_based_copilot(_turn("背景音乐"))
    assert result.actions == []
    assert "UPDATE_AUDIO" in result.reply


def test_bgm_intent_binds_to_selected_clip():
    assert _first(_turn("BGM 小声一点", active="clip-02")).clip_id == "clip-02"


def test_explicit_title_is_extracted():
    action = _first(_turn("标题改成：胰腺癌早期信号"))
    assert action.type == "update_title"
    assert action.title == "胰腺癌早期信号"


# ---------------------------------------------------------------------------
# 安全性：绑定选中的成片 / 有卡片就不重跑 AUTO_EDIT
# ---------------------------------------------------------------------------

def test_actions_bind_to_the_selected_clip_not_the_last_one():
    assert _first(_turn("重新制作封面", active="clip-01")).clip_id == "clip-01"
    assert _first(_turn("重新制作封面", active="clip-02")).clip_id == "clip-02"


def test_rerender_intent_is_blocked_once_clips_exist():
    """已有成片时「重新生成一版」不得启动自动剪辑（否则多出重复卡片）。"""
    result = _rule_based_copilot(_turn("重新生成一版"))
    assert result.actions == []
    assert "重跑自动剪辑" in result.reply


def test_auto_edit_intent_is_blocked_once_clips_exist():
    result = _rule_based_copilot(_turn("开始自动剪辑"))
    assert result.actions == []
    assert "局部任务" in result.reply


def test_auto_edit_is_still_offered_when_no_clips_yet():
    turn = CopilotTurn(message="开始自动剪辑", asr_segments=[{"start": 0, "end": 5, "text": "x"}],
                       duration=30.0)
    result = _rule_based_copilot(turn)
    assert [a.type for a in result.actions] == ["start_auto_edit"]


def test_cover_reply_follows_required_structure():
    """回答结构：识别目标 → 简短计划 → 任务类型 → 可撤销。"""
    result = _rule_based_copilot(_turn("重新制作封面，主题不超过 12 个字"))
    assert "正在修改：剪辑成片 01" in result.reply
    assert "12" in result.reply
    assert "REGENERATE_COVER" in result.reply
    assert "撤销" in result.reply


def test_timeline_intent_without_seconds_asks_instead_of_guessing():
    result = _rule_based_copilot(_turn("改一下时间线"))
    assert result.actions == []
    assert "第 8 秒到第 10 秒" in result.reply
