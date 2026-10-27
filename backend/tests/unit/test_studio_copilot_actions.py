"""Studio Copilot 白名单动作的纯逻辑测试（不连数据库、不调模型）。"""

from src.studio_copilot import (
    CONFIRM_ACTION_TYPES,
    CONFIRM_HINTS,
    CopilotTurn,
    _LlmCopilotAction,
    _normalize_llm_actions,
    _validate_actions,
)

_RENDERED_CLIPS = [
    {"task_id": "autoedit_aaaabbbbcccc", "clip_id": "clip-01", "name": "剪辑成片 01"},
    {"task_id": "autoedit_aaaabbbbcccc", "clip_id": "clip-02", "name": "剪辑成片 02"},
]


def _turn() -> CopilotTurn:
    return CopilotTurn(message="帮我处理成片", rendered_clips=_RENDERED_CLIPS)


def test_make_cover_and_render_clip_are_confirm_only():
    assert {"make_cover", "render_clip"} <= CONFIRM_ACTION_TYPES
    assert "make_cover" in CONFIRM_HINTS and "render_clip" in CONFIRM_HINTS


def test_new_actions_are_forced_confirm_with_real_clip_ids():
    raw_actions = [
        {"type": "make_cover", "label": "重做封面", "execution": "run",
         "task_id": "autoedit_aaaabbbbcccc", "clip_id": "clip-02"},
        {"type": "render_clip", "label": "重渲这条", "execution": "run",
         "task_id": "autoedit_aaaabbbbcccc", "clip_id": "clip-01"},
    ]
    validated, dropped = _validate_actions(raw_actions, _turn())

    assert dropped == []
    assert [item.type for item in validated] == ["make_cover", "render_clip"]
    # 即使模型/规则层声明 execution="run"，二次校验也必须强制 confirm。
    assert all(item.execution == "confirm" for item in validated)
    assert validated[0].clip_id == "clip-02" and validated[0].task_id == "autoedit_aaaabbbbcccc"
    assert validated[1].clip_id == "clip-01"


def test_new_actions_with_fake_clip_ids_are_dropped():
    raw_actions = [
        {"type": "make_cover", "task_id": "autoedit_aaaabbbbcccc", "clip_id": "clip-99"},
        {"type": "render_clip", "task_id": "autoedit_zzzzyyyyxxxx", "clip_id": "clip-01"},
        {"type": "make_cover", "clip_id": "clip-01"},
    ]
    validated, dropped = _validate_actions(raw_actions, _turn())

    assert validated == []
    assert len(dropped) == 3
    assert all("不在当前项目成片列表中" in reason for reason in dropped)


def test_llm_path_forces_confirm_and_filters_arguments():
    actions = [
        _LlmCopilotAction(tool="make_cover", execution="run",
                          arguments={"task_id": "autoedit_aaaabbbbcccc", "clip_id": "clip-01", "oops": 1}),
        _LlmCopilotAction(tool="render_clip",
                          arguments={"task_id": "autoedit_aaaabbbbcccc", "clip_id": "clip-03"}),
    ]
    normalized, dropped = _normalize_llm_actions(_turn(), actions, requires_confirmation=False)

    make_cover = next(item for item in normalized if item.type == "make_cover")
    assert make_cover.execution == "confirm"
    assert make_cover.task_id == "autoedit_aaaabbbbcccc" and make_cover.clip_id == "clip-01"
    assert make_cover.speaker is None and make_cover.index is None
    # clip-03 不在 provided_clips 中 → 二次校验丢弃；未知参数 oops 也被记录。
    assert not [item for item in normalized if item.type == "render_clip"]
    assert len(dropped) >= 2


# ---------------------------------------------------------------------------
# update_bgm：参数二次校验（钳制 / 缺参丢弃 / 成片绑定）
# ---------------------------------------------------------------------------

def test_update_bgm_params_are_clamped_and_bound_to_clip():
    raw = [{"type": "update_bgm", "label": "调BGM", "task_id": "autoedit_aaaabbbbcccc",
            "clip_id": "clip-01", "bgm_volume": 5, "bgm_fade_in_s": -3,
            "bgm_fade_out_s": 99, "bgm_remove": False}]
    validated, dropped = _validate_actions(raw, _turn())

    assert dropped == []
    action = validated[0]
    assert action.bgm_volume == 1.0
    assert action.bgm_fade_in_s == 0.0
    assert action.bgm_fade_out_s == 10.0
    assert action.bgm_remove is False
    assert action.execution == "confirm"
    assert action.task_type == "UPDATE_AUDIO"


def test_update_bgm_without_any_param_is_dropped():
    raw = [{"type": "update_bgm", "label": "调BGM",
            "task_id": "autoedit_aaaabbbbcccc", "clip_id": "clip-01"}]
    validated, dropped = _validate_actions(raw, _turn())

    assert validated == []
    assert len(dropped) == 1
    assert "缺少可执行参数" in dropped[0]
