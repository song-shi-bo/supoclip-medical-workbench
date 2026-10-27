"""医疗词典加载与匹配（medical_lexicon，P1）契约测试。

不调真实模型、不依赖真实词典：临时词典目录用 ``tmp_path`` + ``monkeypatch.setenv``
构造，``MEDICAL_LEXICON_DIR`` 指向临时目录。
核心契约：
* TSV 解析：正常行 / 注释行 / 空行 / 坏行（列数不足）容错，坏行跳过不抛错；
* match_segments：标准词命中、别名命中、按权重降序+标准词排序、limit 截断、空文本；
* 目录不存在 / 空目录 → load 与 match 均返回空结构、绝不抛出；
* 改文件即生效（缓存键随文件 mtime 变化）；
* ``_build_user_prompt``：有命中时在任务说明行之后、全篇摘要之前插入【领域词典】段；
  无命中（含词典模块异常）时 prompt 与无词典模块时逐字节一致，批内文本行保持原样。
"""

from __future__ import annotations

import os

import pytest

from src import medical_lexicon as ml
from src import transcript_correction_llm as tcm


@pytest.fixture(autouse=True)
def _clear_lexicon_cache():
    """隔离 lru_cache：每个用例前后清空，避免跨用例污染。"""
    ml._load_lexicon_cached.cache_clear()
    yield
    ml._load_lexicon_cached.cache_clear()


def _make_lexicon(tmp_path: object, content: str, name: str = "core.tsv"):
    """在临时目录构造词典：返回词典目录路径。"""
    directory = tmp_path / "medical_lexicon"  # type: ignore[operator]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(content, encoding="utf-8")
    return directory


def _set_env(monkeypatch: pytest.MonkeyPatch, directory) -> None:
    monkeypatch.setenv("MEDICAL_LEXICON_DIR", str(directory))


# ---------------------------------------------------------------------------
# 1. TSV 解析：正常行 / 注释行 / 空行 / 坏行容错
# ---------------------------------------------------------------------------


def test_load_lexicon_parses_rows_and_skips_bad_lines(tmp_path, monkeypatch: pytest.MonkeyPatch):
    directory = _make_lexicon(tmp_path, (
        "# 这是注释行，应跳过\n"
        "\n"
        "   \n"
        "头孢\tdrug\t头包|孢头\t9\t头孢类抗生素\n"
        "化疗\tproc\t话聊\t5\n"          # 无备注列
        "ct\texam\t\t3\t\n"             # 别名列为空 → aliases=[]
        "只两列\tdrug\n"                # 坏行：列数不足 3 → 跳过
        "\tdrug\t别名\n"                # 坏行：标准词为空 → 跳过
    ))
    _set_env(monkeypatch, directory)
    data = ml.load_lexicon()

    assert [entry["standard"] for entry in data["entries"]] == ["头孢", "化疗", "ct"]
    first = data["entries"][0]
    assert first == {"standard": "头孢", "type": "drug", "aliases": ["头包", "孢头"],
                     "weight": 9, "note": "头孢类抗生素"}
    assert data["entries"][1] == {"standard": "化疗", "type": "proc", "aliases": ["话聊"],
                                  "weight": 5, "note": ""}
    assert data["entries"][2]["aliases"] == []
    # alias_map：标准词与别名都映射到标准词；坏行/注释行不产生映射
    assert data["alias_map"] == {
        "头孢": "头孢", "头包": "头孢", "孢头": "头孢",
        "化疗": "化疗", "话聊": "化疗",
        "ct": "ct",
    }


def test_load_lexicon_weight_defaults_and_clamps(tmp_path, monkeypatch: pytest.MonkeyPatch):
    directory = _make_lexicon(tmp_path, (
        "甲\tdrug\t\n"        # 权重缺失 → 默认 5
        "乙\tdrug\t\tabc\n"   # 权重不可解析 → 默认 5
        "丙\tdrug\t\t0\n"     # 低于下限 → 夹取 1
        "丁\tdrug\t\t99\n"    # 超过上限 → 夹取 9
    ))
    _set_env(monkeypatch, directory)
    weights = {entry["standard"]: entry["weight"] for entry in ml.load_lexicon()["entries"]}
    assert weights == {"甲": 5, "乙": 5, "丙": 1, "丁": 9}


def test_load_lexicon_multiple_tsv_files_merged(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _make_lexicon(tmp_path, "头孢\tdrug\t头包\t9\t\n")
    _make_lexicon(tmp_path, "化疗\tproc\t话聊\t5\t\n", name="extra.tsv")
    _set_env(monkeypatch, tmp_path / "medical_lexicon")
    data = ml.load_lexicon()
    assert [entry["standard"] for entry in data["entries"]] == ["头孢", "化疗"]  # 文件名序
    assert len(data["alias_map"]) == 4


def test_load_lexicon_cache_refreshed_when_file_content_changes(
        tmp_path, monkeypatch: pytest.MonkeyPatch):
    directory = _make_lexicon(tmp_path, "头孢\tdrug\t头包\t9\t\n")
    _set_env(monkeypatch, directory)
    assert len(ml.load_lexicon()["entries"]) == 1

    # 原地改写文件内容（显式 utime 保证 mtime 必然前进，不依赖文件系统精度）
    target = directory / "core.tsv"
    target.write_text("头孢\tdrug\t头包\t9\t\n化疗\tproc\t话聊\t5\t\n", encoding="utf-8")
    later = target.stat().st_mtime_ns + 1_000_000
    os.utime(target, ns=(later, later))
    data = ml.load_lexicon()
    assert [entry["standard"] for entry in data["entries"]] == ["头孢", "化疗"]


def test_load_lexicon_never_raises_on_io_error(tmp_path, monkeypatch: pytest.MonkeyPatch):
    directory = _make_lexicon(tmp_path, "占位\n")
    (directory / "bad.tsv").write_bytes(b"\xff\xfe not utf-8")  # 解码必炸的字节
    _set_env(monkeypatch, directory)
    assert ml.load_lexicon() == {"entries": [], "alias_map": {}}
    assert ml.match_segments(["任意文本"]) == []


# ---------------------------------------------------------------------------
# 2. match_segments：命中 / 排序 / 截断 / 空文本
# ---------------------------------------------------------------------------


def test_match_segments_hits_standard_and_alias(tmp_path, monkeypatch: pytest.MonkeyPatch):
    directory = _make_lexicon(tmp_path, (
        "头孢\tdrug\t头包\t9\t头孢类抗生素\n"
        "化疗\tproc\t话聊\t5\t\n"
    ))
    _set_env(monkeypatch, directory)

    # 标准词命中
    hits = ml.match_segments(["患者停用头孢后好转"])
    assert [hit["standard"] for hit in hits] == ["头孢"]
    assert hits[0] == {"standard": "头孢", "type": "drug", "aliases": ["头包"],
                       "weight": 9, "note": "头孢类抗生素"}
    # 别名命中（跨段拼接匹配：别名在第 2 段）
    hits = ml.match_segments(["这段没有词典词", "医生建议话聊疏导"])
    assert [hit["standard"] for hit in hits] == ["化疗"]
    # 无命中
    assert ml.match_segments(["今天天气不错"]) == []


def test_match_segments_sorts_by_weight_then_standard(tmp_path, monkeypatch: pytest.MonkeyPatch):
    directory = _make_lexicon(tmp_path, (
        "阿司匹林\tdrug\t\t3\t\n"
        "布洛芬\tdrug\t\t9\t\n"
        "头孢\tdrug\t\t9\t\n"
    ))
    _set_env(monkeypatch, directory)
    text = "布洛芬与头孢与阿司匹林"
    hits = ml.match_segments([text])
    assert [hit["standard"] for hit in hits] == ["头孢", "布洛芬", "阿司匹林"]


def test_match_segments_truncates_to_limit(tmp_path, monkeypatch: pytest.MonkeyPatch):
    rows = "".join(f"词{i}\tdrug\t\t5\t\n" for i in range(5))
    _set_env(monkeypatch, _make_lexicon(tmp_path, rows))
    text = "词0词1词2词3词4"
    assert len(ml.match_segments([text])) == 5
    assert [hit["standard"] for hit in ml.match_segments([text], limit=2)] == ["词0", "词1"]
    assert ml.match_segments([text], limit=0) == []


def test_match_segments_empty_texts_return_empty(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _set_env(monkeypatch, _make_lexicon(tmp_path, "头孢\tdrug\t头包\t9\t\n"))
    assert ml.match_segments([]) == []
    assert ml.match_segments(["", "   "]) == []


# ---------------------------------------------------------------------------
# 3. 目录不存在 / 空目录 → 空结构、不抛错
# ---------------------------------------------------------------------------


def test_missing_directory_returns_empty_and_never_raises(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _set_env(monkeypatch, tmp_path / "does_not_exist")
    assert ml.load_lexicon() == {"entries": [], "alias_map": {}}
    assert ml.match_segments(["患者有高血压"]) == []


def test_empty_directory_returns_empty_and_never_raises(tmp_path, monkeypatch: pytest.MonkeyPatch):
    directory = tmp_path / "medical_lexicon"
    directory.mkdir()
    _set_env(monkeypatch, directory)
    assert ml.load_lexicon() == {"entries": [], "alias_map": {}}
    assert ml.match_segments(["患者有高血压"]) == []


def test_lexicon_dir_env_overrides_default(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEDICAL_LEXICON_DIR", str(tmp_path / "custom"))
    assert ml.lexicon_dir() == tmp_path / "custom"
    monkeypatch.delenv("MEDICAL_LEXICON_DIR")
    assert ml.lexicon_dir() == ml._DEFAULT_LEXICON_DIR


# ---------------------------------------------------------------------------
# 4. _build_user_prompt：词典段插入 / 无命中逐字节一致 / 文本行保持原样
# ---------------------------------------------------------------------------


def _prepared(n: int) -> list[dict]:
    return [{"start": float(i), "end": float(i) + 1.5, "text": f"文本{i}"} for i in range(n)]


def test_prompt_without_hits_is_byte_identical_to_no_lexicon(
        monkeypatch: pytest.MonkeyPatch):
    """无命中时 prompt 与「无词典模块时」逐字节一致（分批与整篇两种模式）。"""
    monkeypatch.setattr(ml, "match_segments", lambda texts, **kwargs: [])
    monkeypatch.setattr(tcm, "SUMMARY_CHARS", 6)  # 缩短摘要便于精确断言

    # 分批模式：任务说明行 → 全篇摘要 → 前文重叠 → 待校对分段（与改造前实现一致）
    prepared = _prepared(8)
    batch = prepared[3:5]
    expected = "\n".join([
        "请校对以下 2 段转写（全局序号自 3 起）。只输出发生修改的分段。",
        "【全篇摘要（截取自开头，仅供理解，勿输出）】文本0文本1",
        "【前文重叠（仅供上下文，勿输出）】0|文本0 1|文本1 2|文本2",
        "【待校对分段（序号|文本；只返回有修改的 patch）】",
        "3|文本3",
        "4|文本4",
    ])
    assert tcm._build_user_prompt(batch, 3, prepared, batched_mode=True) == expected

    # 整篇模式：无摘要/重叠
    prepared_small = _prepared(2)
    expected_single = "\n".join([
        "请校对以下 2 段转写（全局序号自 0 起）。只输出发生修改的分段。",
        "【待校对分段（序号|文本；只返回有修改的 patch）】",
        "0|文本0",
        "1|文本1",
    ])
    assert tcm._build_user_prompt(prepared_small, 0, prepared_small,
                                  batched_mode=False) == expected_single


def test_prompt_inserts_lexicon_section_between_intro_and_summary(
        monkeypatch: pytest.MonkeyPatch):
    """有命中时：任务说明行之后、【全篇摘要】之前插入词典段，格式逐字符断言。"""
    seen: dict = {}

    def fake_match(texts, *, limit=20):
        seen["texts"] = list(texts)
        seen["limit"] = limit
        return [
            {"standard": "头孢", "type": "drug", "aliases": ["头包"], "weight": 9, "note": ""},
            {"standard": "化疗", "type": "proc", "aliases": ["话聊"], "weight": 5, "note": ""},
        ]

    monkeypatch.setattr(ml, "match_segments", fake_match)
    prepared = _prepared(5)
    batch = prepared[2:4]
    prompt = tcm._build_user_prompt(batch, 2, prepared, batched_mode=True)

    lines = prompt.splitlines()
    assert lines[0] == "请校对以下 2 段转写（全局序号自 2 起）。只输出发生修改的分段。"
    assert lines[1] == ("【领域词典（仅供校对参考；仅当原文词与条目同音/形近且语义吻合才修改；"
                        "不确定保持原文并标 uncertain；不得凭空引入词典外的修改）】")
    assert lines[2] == "  头孢（drug）常见错写：头包"
    assert lines[3] == "  化疗（proc）常见错写：话聊"
    assert lines[4].startswith("【全篇摘要")
    # 词典匹配吃的是批内文本、固定 limit=20
    assert seen["texts"] == ["文本2", "文本3"]
    assert seen["limit"] == 20
    # 批内文本行保持原样，仍位于「待校对分段」区内
    assert "2|文本2" in prompt and "3|文本3" in prompt


def test_prompt_entry_without_aliases_omits_mistakes_part(monkeypatch: pytest.MonkeyPatch):
    """无别名的条目省略「常见错写」部分。"""
    monkeypatch.setattr(ml, "match_segments", lambda texts, **kwargs: [
        {"standard": "阿司匹林", "type": "drug", "aliases": [], "weight": 5, "note": "备注不进 prompt"},
    ])
    prepared = _prepared(1)
    prompt = tcm._build_user_prompt(prepared, 0, prepared, batched_mode=False)
    assert "  阿司匹林（drug）\n" in prompt
    assert "常见错写" not in prompt
    assert "备注不进 prompt" not in prompt


def test_prompt_lexicon_exception_falls_back_to_baseline(monkeypatch: pytest.MonkeyPatch):
    """词典匹配抛异常时吞掉，prompt 与无词典时逐字节一致。"""
    monkeypatch.setattr(tcm, "SUMMARY_CHARS", 6)

    def boom(texts, *, limit=20):
        raise RuntimeError("词典炸了")

    monkeypatch.setattr(ml, "match_segments", boom)
    prepared = _prepared(8)
    batch = prepared[3:5]
    prompt = tcm._build_user_prompt(batch, 3, prepared, batched_mode=True)
    assert "领域词典" not in prompt
    assert prompt.startswith("请校对以下 2 段转写（全局序号自 3 起）")
    assert "3|文本3" in prompt and "4|文本4" in prompt


def test_prompt_with_real_tmp_lexicon_end_to_end(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """真实临时词典 → match_segments → prompt 含词典段（不走桩）。"""
    _set_env(monkeypatch, _make_lexicon(tmp_path, "头孢\tdrug\t头包\t9\t头孢类抗生素\n"))
    batch = [{"start": 0.0, "end": 1.0, "text": "患者头包过敏"},
             {"start": 1.0, "end": 2.0, "text": "改用其他药物"}]
    prompt = tcm._build_user_prompt(batch, 0, batch, batched_mode=False)
    assert "【领域词典（仅供校对参考" in prompt
    assert "  头孢（drug）常见错写：头包" in prompt
    assert "0|患者头包过敏" in prompt and "1|改用其他药物" in prompt
