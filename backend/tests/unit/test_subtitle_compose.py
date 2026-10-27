"""subtitle_compose 单元测试：ASS/SRT 文本、百分比几何、版本目录与原子写。

不连数据库、不联网、不调 ffmpeg。覆盖：
1. percent_to_pixels 精确像素值（默认 y_pct=78.125 → MarginV 420）；
2. ASS 头逐字节断言（与 video-brain app/subtitles.py 兼容）；
3. 颜色 #RRGGBB → &H00BBGGRR、背景开启 → BorderStyle 3 + alpha；
4. word_highlight 的 \\k 卡拉OK标签（时长均分）与 \\an2；
5. build_cues 的 text/start/end 覆盖 + 无匹配追加额外 cue + 空文本跳过；
6. SRT 格式；
7. compose 两次 → rev-001 / rev-002，内容不同，meta.json 可追溯；
8. 写失败不留下半个文件；
9. 配置非法 / 无 cue 的显式 E_ 码。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import subtitle_compose as sc

# video-brain/app/subtitles.py 生成的头部（同 FONT_NAME/FONT_SIZE/MARGIN_V/Alignment）：
EXPECTED_HEAD = (
    "[Script Info]\n"
    "ScriptType: v4.00+\n"
    "PlayResX: 1080\n"
    "PlayResY: 1920\n"
    "WrapStyle: 0\n"
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
    "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
    "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    "Style: Speak,Noto Sans CJK SC,44,&H0000D7FF,&H000000FF,&H00101010,&H80000000,"
    "1,0,0,0,100,100,0,0,1,3,1,2,70,70,420,1\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)


def _cues() -> list[dict]:
    return sc.build_cues([
        {"segment_id": "seg-1", "start": 0.0, "end": 2.0, "text": "你好，世界", "speaker": "医生"},
        {"segment_id": "seg-2", "start": 2.5, "end": 5.25, "text": "第二句", "speaker": "患者"},
    ])


# ---------------------------------------------------------------------------
# 1. 百分比 → 像素
# ---------------------------------------------------------------------------

def test_percent_to_pixels_matches_contract_example():
    assert sc.percent_to_pixels({"x_pct": 50, "y_pct": 78.125}, 1080, 1920) == {"x": 540, "bottom": 420}


def test_percent_to_pixels_defaults_and_edges():
    # 缺字段 → 默认底部居中（MarginV=420，与 video-brain 一致）。
    assert sc.percent_to_pixels({}) == {"x": 540, "bottom": 420}
    assert sc.percent_to_pixels(None) == {"x": 540, "bottom": 420}
    assert sc.percent_to_pixels({"y_pct": 100}, 1080, 1920)["bottom"] == 0
    assert sc.percent_to_pixels({"x_pct": 0}, 1080, 1920)["x"] == 0
    assert sc.percent_to_pixels({"x_pct": 25}, 1080, 1920)["x"] == 270
    # 越界钳制、非数字退回默认（纯几何助手不抛错）。
    assert sc.percent_to_pixels({"x_pct": 999}, 1080, 1920)["x"] == 1080
    assert sc.percent_to_pixels({"x_pct": "左"})["x"] == 540
    # 自定义画布尺寸。
    assert sc.percent_to_pixels({"x_pct": 50, "y_pct": 50}, 1080, 1080) == {"x": 540, "bottom": 540}


def test_default_y_pct_equals_margin_v_420():
    assert sc.DEFAULT_Y_PCT == 78.125
    assert sc.percent_to_pixels({"y_pct": sc.DEFAULT_Y_PCT}, 1080, 1920)["bottom"] == 420


# ---------------------------------------------------------------------------
# 2. ASS 头 / Dialogue
# ---------------------------------------------------------------------------

def test_ass_head_is_video_brain_compatible():
    content = sc.ass_content(_cues(), {})
    assert content.startswith(EXPECTED_HEAD)


def test_ass_dialogue_lines_exact_when_position_is_centered():
    content = sc.ass_content(_cues(), {})
    lines = [line for line in content.splitlines() if line.startswith("Dialogue:")]
    assert lines == [
        "Dialogue: 0,0:00:00.00,0:00:02.00,Speak,,0,0,0,,你好，世界",
        "Dialogue: 0,0:00:02.50,0:00:05.25,Speak,,0,0,0,,第二句",
    ]
    # 没有词级高亮、位置居中时，不该出现任何内联覆盖标签。
    assert "{" not in content.split("[Events]")[1]


def test_ass_uses_config_font_colors_and_margins():
    content = sc.ass_content(_cues(), {
        "font_size_px": 72,
        "font_family": "Source Han Sans",
        "text_color": "#00FF00",
        "outline_color": "#0000FF",
        "outline_width": 6,
        "position": {"x_pct": 50, "y_pct": 50},
    })
    assert ("Style: Speak,Source Han Sans,72,&H0000FF00,&H000000FF,&H00FF0000,&H80000000,"
            "1,0,0,0,100,100,0,0,1,6,1,2,70,70,960,1") in content


def test_ass_background_enables_border_style_3_and_alpha():
    content = sc.ass_content(_cues(), {
        "background": {"enabled": True, "color": "#FF0000", "opacity": 0.5},
    })
    # opacity 0.5 → alpha 0x80；#FF0000 → &H0000FF。
    assert "1,0,0,0,100,100,0,0,3,3,1,2,70,70,420,1" in content
    assert ",&H800000FF," in content

    opaque = sc.ass_content(_cues(), {"background": {"enabled": True, "color": "#000000", "opacity": 1.0}})
    assert ",&H00000000," in opaque


def test_ass_position_offset_emits_pos_anchor():
    content = sc.ass_content(_cues(), {"position": {"x_pct": 25.0, "y_pct": 78.125}})
    assert "{\\an2\\pos(270,1500)}你好，世界" in content


def test_ass_escapes_braces_and_newlines():
    cues = sc.build_cues([{"segment_id": "s", "start": 0, "end": 1, "text": "换行\n{大括号}"}])
    content = sc.ass_content(cues, {})
    # 2026-10-19 起文本内换行保留为 ASS 硬折行 \N（旧行为：展平成空格）
    assert "换行\\N\\{大括号\\}" in content


# ---------------------------------------------------------------------------
# 3. word_highlight（无词级时间戳 → 均分）
# ---------------------------------------------------------------------------

def test_word_highlight_distributes_cue_duration_evenly():
    cues = sc.build_cues([{"segment_id": "s", "start": 1.0, "end": 3.0, "text": "你好"}])
    content = sc.ass_content(cues, {"word_highlight": True})
    dialogue = next(line for line in content.splitlines() if line.startswith("Dialogue:"))
    # 2s → 200cs，两个字各 100cs；\an2 保留。
    assert dialogue.endswith("{\\an2\\k100}你{\\k100}好")


def test_word_highlight_tokenizes_latin_words_and_keeps_spaces():
    cues = sc.build_cues([{"segment_id": "s", "start": 0.0, "end": 1.0, "text": "hello world"}])
    content = sc.ass_content(cues, {"word_highlight": True})
    dialogue = next(line for line in content.splitlines() if line.startswith("Dialogue:"))
    # 100cs 均分给 2 个 token，空格并入前一个 token（不会把词连在一起）。
    assert dialogue.endswith("{\\an2\\k50}hello {\\k50}world")


def test_word_highlight_spans_sum_to_the_full_cue_duration():
    cues = sc.build_cues([{"segment_id": "s", "start": 0.0, "end": 1.234, "text": "一二三四五"}])
    content = sc.ass_content(cues, {"word_highlight": True})
    dialogue = next(line for line in content.splitlines() if line.startswith("Dialogue:"))
    spans = [int(value.split("}")[0]) for value in dialogue.split("\\k")[1:]]
    assert len(spans) == 5
    assert sum(spans) == round(1.234 * 100)


def test_word_highlight_false_has_no_karaoke_tags():
    cues = sc.build_cues([{"segment_id": "s", "start": 0.0, "end": 1.0, "text": "你好"}])
    assert "\\k" not in sc.ass_content(cues, {"word_highlight": False})


# ---------------------------------------------------------------------------
# 4. build_cues
# ---------------------------------------------------------------------------

def test_build_cues_sorts_indexes_and_skips_empty_text():
    cues = sc.build_cues([
        {"segment_id": "b", "start": 5.0, "end": 6.0, "text": "第二"},
        {"segment_id": "a", "start": 1.0, "end": 2.0, "text": "第一"},
        {"segment_id": "c", "start": 7.0, "end": 8.0, "text": "   "},
    ])
    assert [cue["index"] for cue in cues] == [1, 2]
    assert [cue["segment_id"] for cue in cues] == ["a", "b"]
    assert [cue["text"] for cue in cues] == ["第一", "第二"]


def test_build_cues_applies_text_and_time_overrides():
    cues = sc.build_cues(
        [{"segment_id": "seg-1", "start": 0.0, "end": 2.0, "text": "原始文案", "speaker": "未标注"}],
        overrides=[
            {"segment_id": "seg-1", "text": "改过的文案", "start": 1.0, "end": 3.0,
             "speaker": "医生", "updated_at": "2026-10-13T00:00:00Z"},
        ],
    )
    assert cues == [{
        "index": 1, "segment_id": "seg-1", "start": 1.0, "end": 3.0,
        "text": "改过的文案", "speaker": "医生",
    }]


def test_build_cues_appends_unmatched_override_as_extra_cue():
    cues = sc.build_cues(
        [{"segment_id": "seg-1", "start": 0.0, "end": 2.0, "text": "第一句"}],
        overrides=[{"segment_id": "manual-1", "start": 10.0, "end": 12.0, "text": "手工补充"}],
    )
    assert [cue["segment_id"] for cue in cues] == ["seg-1", "manual-1"]
    assert cues[-1]["text"] == "手工补充"
    assert cues[-1]["start"] == 10.0 and cues[-1]["end"] == 12.0

    # 完全没有 segment_id 的手工 cue 会被补一个可读 id。
    unnamed = sc.build_cues([], overrides=[{"start": 1.0, "end": 2.0, "text": "补充"}])
    assert unnamed[0]["segment_id"] == "manual-001"

    # 只给 start 没有 end 的 override 既覆盖不了也追加不了 → 忽略。
    assert sc.build_cues([], overrides=[{"start": 1.0, "text": "半条"}]) == []


def test_build_cues_override_can_fill_an_otherwise_empty_segment():
    cues = sc.build_cues(
        [{"segment_id": "seg-1", "start": 0.0, "end": 2.0, "text": ""}],
        overrides=[{"segment_id": "seg-1", "text": "手填文本"}],
    )
    assert [cue["text"] for cue in cues] == ["手填文本"]


def test_build_cues_guards_zero_length_cue():
    cues = sc.build_cues([{"segment_id": "s", "start": 3.0, "end": 3.0, "text": "瞬时"}])
    assert cues[0]["end"] == 3.5


# ---------------------------------------------------------------------------
# 5. SRT
# ---------------------------------------------------------------------------

def test_srt_content_format():
    content = sc.srt_content(_cues())
    assert content == (
        "1\n00:00:00,000 --> 00:00:02,000\n你好，世界\n\n"
        "2\n00:00:02,500 --> 00:00:05,250\n第二句\n\n"
    )


def test_srt_content_reindexes_when_index_missing():
    content = sc.srt_content([{"start": 1.0, "end": 2.0, "text": "a"},
                              {"start": 2.0, "end": 3.0, "text": "b"}])
    assert content.startswith("1\n00:00:01,000 --> 00:00:02,000\na\n\n2\n")


# ---------------------------------------------------------------------------
# 6. 版本目录 / compose
# ---------------------------------------------------------------------------

def test_revision_dir_layout():
    clip_dir = Path("/tmp/tasks/output/autoedit_x/clip-1")
    assert sc.revision_dir(clip_dir, 1) == clip_dir / "subtitles" / "rev-001"
    assert sc.revision_dir(clip_dir, 12) == clip_dir / "subtitles" / "rev-012"
    assert sc.revision_dir(clip_dir, 1234) == clip_dir / "subtitles" / "rev-1234"


def test_list_revisions_and_next_revision_tolerate_missing_and_corrupt(tmp_path):
    clip_dir = tmp_path / "clip"
    assert sc.list_revisions(clip_dir) == []
    assert sc.next_revision(clip_dir) == 1

    first = sc.compose(clip_dir, clip_id="clip-1", cues=_cues(), config={})
    assert first["revision"] == 1
    assert sc.next_revision(clip_dir) == 2

    # 损坏的 meta.json 被跳过，但已有版本目录仍然占号（绝不重用）。
    (sc.revision_dir(clip_dir, 2)).mkdir(parents=True)
    (sc.revision_dir(clip_dir, 2) / sc.META_FILENAME).write_text("{坏", encoding="utf-8")
    assert [item["revision"] for item in sc.list_revisions(clip_dir)] == [1]
    assert sc.next_revision(clip_dir) == 3


def test_compose_writes_three_files_atomically(tmp_path):
    clip_dir = tmp_path / "clip"
    result = sc.compose(clip_dir, clip_id="clip-1", cues=_cues(), config={})
    directory = sc.revision_dir(clip_dir, 1)

    assert result["revision"] == 1
    assert result["dir"] == str(directory)
    assert result["ass_path"] == str(directory / "clip-1.ass")
    assert result["srt_path"] == str(directory / "clip-1.srt")
    assert result["cue_count"] == 2
    assert result["margin_v"] == 420
    assert result["font_size_px"] == sc.DEFAULT_FONT_SIZE
    assert result["style_id"] == "workbench_v1"

    assert (directory / "clip-1.ass").read_text(encoding="utf-8").startswith(EXPECTED_HEAD)
    assert (directory / "clip-1.srt").read_text(encoding="utf-8").startswith("1\n00:00:00,000")
    meta = json.loads((directory / sc.META_FILENAME).read_text(encoding="utf-8"))
    assert meta["revision"] == 1 and meta["cue_count"] == 2
    assert meta["margin_v"] == 420 and meta["style_id"] == "workbench_v1"
    assert meta["ass_rel"] == "subtitles/rev-001/clip-1.ass"
    assert meta["srt_rel"] == "subtitles/rev-001/clip-1.srt"
    assert meta["config_hash"] == result["config_hash"]
    assert meta["config"]["position"]["y_pct"] == sc.DEFAULT_Y_PCT

    # 目录里只有三件套，没有 .tmp 残留、也没有第四个文件。
    assert sorted(path.name for path in directory.iterdir()) == sorted(
        ["clip-1.ass", "clip-1.srt", sc.META_FILENAME])


def test_two_consecutive_composes_create_two_versions(tmp_path):
    clip_dir = tmp_path / "clip"
    first = sc.compose(clip_dir, clip_id="clip-1", cues=_cues(), config={})
    second = sc.compose(
        clip_dir, clip_id="clip-1",
        cues=sc.build_cues([{"segment_id": "seg-1", "start": 0.0, "end": 2.0, "text": "换了一版"}]),
        config={"font_size_px": 80},
    )

    assert first["revision"] == 1 and second["revision"] == 2
    assert Path(first["ass_path"]).is_file() and Path(second["ass_path"]).is_file()
    assert Path(first["ass_path"]).parent != Path(second["ass_path"]).parent
    old_text = Path(first["ass_path"]).read_text(encoding="utf-8")
    new_text = Path(second["ass_path"]).read_text(encoding="utf-8")
    assert "你好，世界" in old_text and "换了一版" not in old_text
    assert "换了一版" in new_text
    assert first["config_hash"] != second["config_hash"]
    assert [item["revision"] for item in sc.list_revisions(clip_dir)] == [1, 2]


def test_compose_explicit_revision_is_never_reused_by_next(tmp_path):
    clip_dir = tmp_path / "clip"
    sc.compose(clip_dir, clip_id="clip-1", cues=_cues(), config={}, revision=5)
    assert sc.next_revision(clip_dir) == 6
    assert sc.list_revisions(clip_dir)[0]["revision"] == 5


def test_compose_leaves_no_partial_files_when_writing_fails(tmp_path, monkeypatch):
    clip_dir = tmp_path / "clip"

    def explode(_cues, *args, **kwargs):  # srt_content 已扩展 config/width/height 参数
        raise RuntimeError("模拟 SRT 生成失败")

    monkeypatch.setattr(sc, "srt_content", explode)
    with pytest.raises(RuntimeError):
        sc.compose(clip_dir, clip_id="clip-1", cues=_cues(), config={})

    # 失败的这一版目录整体消失，不会留下半个文件，也不会占号。
    assert not sc.revision_dir(clip_dir, 1).exists()
    assert sc.list_revisions(clip_dir) == []
    assert sc.next_revision(clip_dir) == 1


def test_compose_failure_does_not_touch_existing_versions(tmp_path, monkeypatch):
    clip_dir = tmp_path / "clip"
    good = sc.compose(clip_dir, clip_id="clip-1", cues=_cues(), config={})

    monkeypatch.setattr(sc, "ass_content", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("boom")))
    with pytest.raises(ValueError):
        sc.compose(clip_dir, clip_id="clip-1", cues=_cues(), config={})

    assert Path(good["ass_path"]).is_file()
    assert [item["revision"] for item in sc.list_revisions(clip_dir)] == [1]


# ---------------------------------------------------------------------------
# 7. 错误码
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("config", [
    {"font_size_px": "62"},
    {"font_size_px": 0},
    {"outline_width": 13},
    {"outline_width": -1},
    {"text_color": "white"},
    {"outline_color": "#12345"},
    {"background": "yes"},
    {"background": {"enabled": "yes"}},
    {"background": {"opacity": 1.5}},
    {"position": "bottom"},
    {"position": {"x_pct": 101}},
    {"position": {"y_pct": True}},
    {"word_highlight": "true"},
    {"font_family": 7},
    "not-a-dict",
])
def test_invalid_config_raises_with_code(config):
    with pytest.raises(sc.SubtitleComposeError) as excinfo:
        sc.normalize_config(config)
    assert excinfo.value.code == "E_SUBTITLE_CONFIG_INVALID"


def test_compose_without_cues_raises_no_cues(tmp_path):
    with pytest.raises(sc.SubtitleComposeError) as excinfo:
        sc.compose(tmp_path / "clip", clip_id="clip-1", cues=[], config={})
    assert excinfo.value.code == "E_SUBTITLE_NO_CUES"

    blank = sc.build_cues([{"segment_id": "s", "start": 0.0, "end": 1.0, "text": "  "}])
    with pytest.raises(sc.SubtitleComposeError) as blank_error:
        sc.compose(tmp_path / "clip", clip_id="clip-1", cues=blank, config={})
    assert blank_error.value.code == "E_SUBTITLE_NO_CUES"


def test_compose_rejects_unsafe_clip_id(tmp_path):
    with pytest.raises(sc.SubtitleComposeError) as excinfo:
        sc.compose(tmp_path / "clip", clip_id="../escape", cues=_cues(), config={})
    assert excinfo.value.code == "E_SUBTITLE_CONFIG_INVALID"


def test_config_hash_is_stable_and_defaults_are_explicit():
    assert sc.config_hash({}) == sc.config_hash(sc.normalize_config({}))
    assert sc.config_hash({}) == sc.config_hash({"font_size_px": 44, "word_highlight": False})
    assert sc.config_hash({"font_size_px": 63}) != sc.config_hash({})


# ---------------------------------------------------------------------------
# 确定性折行（宽度感知）：libass 对纯 CJK 长行不自动折，必须显式 \N
# ---------------------------------------------------------------------------

def test_wrap_limit_is_width_and_font_aware():
    # 默认画布：1080-140=940，字号 44 → 每行 21 字；配置 14 只能取到 14
    assert sc._wrap_limit(sc.normalize_config({}), 1080) == 14
    assert sc._wrap_limit(sc.normalize_config({"max_chars_per_line": 10}), 1080) == 10
    # 显式关闭
    assert sc._wrap_limit(sc.normalize_config({"max_chars_per_line": 0}), 1080) == 0
    # 小画布 + 大字号 → 至少 1
    assert sc._wrap_limit(sc.normalize_config({"font_size_px": 400}), 200) == 1


def test_wrap_display_lines_breaks_cjk_and_keeps_ascii_words():
    lines = sc._wrap_display_lines("一二三四五六七八九十甲乙丙丁戊", 14)
    assert lines == ["一二三四五六七八九十甲乙丙丁", "戊"]
    # ASCII 词（含数值单位 120/80、mmHg）不被拆断
    lines = sc._wrap_display_lines("血压 mmHg 数值 120/80 正常", 8)
    assert any("mmHg" in line for line in lines)
    assert any("120/80" in line for line in lines)
    # 已有换行视为硬折
    assert sc._wrap_display_lines("甲乙\n丙丁", 10) == ["甲乙", "丙丁"]


def test_ass_long_cjk_cue_is_wrapped_within_margins():
    long_text = "生产样式验证：字幕应贴在底部安全区，字号六十二、位置百分之七十八。"
    cue = {"start": 0.2, "end": 2.8, "text": long_text}
    content = sc.ass_content([cue], {})
    dialogue = [line for line in content.splitlines() if line.startswith("Dialogue:")][0]
    body = dialogue.rsplit(",,", 1)[1]  # Text 字段在最后一个「,,」之后
    assert "\\N" in body  # 显式折行
    limit = sc._wrap_limit(sc.normalize_config({}), 1080)
    for line in body.split("\\N"):
        assert len(line) <= limit


def test_ass_wrap_disabled_when_max_chars_zero():
    cue = {"start": 0.2, "end": 2.8, "text": "一二三四五六七八九十甲乙丙丁戊己"}
    content = sc.ass_content([cue], {"max_chars_per_line": 0})
    assert "\\N" not in content


def test_word_highlight_wrap_keeps_inline_tags_only_on_first_block():
    cue = {"start": 0.0, "end": 2.0, "text": "一二三四五六七八九十甲乙丙丁戊己庚辛壬子"}
    content = sc.ass_content([cue], {"word_highlight": True})
    dialogue = [line for line in content.splitlines() if line.startswith("Dialogue:")][0]
    assert dialogue.count("{\\an2") == 1  # inline 标签只出现在首个 block（前缀匹配）
    assert "\\N" in dialogue


def test_srt_content_wraps_like_ass():
    cue = {"start": 0.2, "end": 2.8, "text": "一二三四五六七八九十甲乙丙丁戊己庚辛壬子丑"}
    srt = sc.srt_content([cue], config={}, width=1080)
    assert "\n" in srt.split("-->")[1].split("\n", 1)[1]
    assert sc.srt_content([{"start": 0.0, "end": 1.0, "text": "短句"}], config={}).count("短句") == 1


def test_compose_writes_wrapped_ass_and_srt(tmp_path):
    long_cue = [{"start": 0.2, "end": 2.8, "text": "这是一条特别长的中文验证字幕用来确认折行在工作台合成时同样生效没错就是这么长。"}]
    meta = sc.compose(tmp_path, clip_id="clip_x", cues=long_cue, config={})
    ass_text = Path(meta["ass_path"]).read_text(encoding="utf-8")
    srt_text = Path(meta["srt_path"]).read_text(encoding="utf-8")
    assert "\\N" in ass_text
    body = [l for l in ass_text.splitlines() if l.startswith("Dialogue:")][0].rsplit(",,", 1)[1]
    limit = sc._wrap_limit(sc.normalize_config({}), 1080)
    assert all(len(line) <= limit for line in body.split("\\N"))
    # SRT 同样折行：cue 文本占多行（编号/时间轴各一行之外还有折行行）
    assert len(srt_text.strip().splitlines()) > 3
