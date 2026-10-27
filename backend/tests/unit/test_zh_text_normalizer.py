"""zh_text_normalizer 单元测试：繁→简规整与失败兜底姿态。

背景：faster-whisper 中文输出繁简混排（2026-10-15/17 实测复发），
ASR 结果入口统一 t2s 规整，下游只见简体。
"""

from __future__ import annotations

import pytest

from src import zh_text_normalizer as ztn


class TestToSimplified:
    def test_classical_traditional_converted(self):
        assert ztn.to_simplified("醫學檢查報告") == "医学检查报告"
        assert ztn.to_simplified("這裡的藥物劑量") == "这里的药物剂量"

    def test_real_asr_sample_from_20261017(self):
        # 10-17 合成验收转写 seg_0006 原文（LLM 纠错当时未处理繁体）
        raw = "第三,把複雜任務拆成幾個可以完成的小步驟,每一步結束後記錄結果。"
        out = ztn.to_simplified(raw)
        assert out == "第三,把复杂任务拆成几个可以完成的小步骤,每一步结束后记录结果。"

    def test_simplified_text_unchanged(self):
        text = "这是一段工作台验收用的合成语音，仅用于流程验证。"
        assert ztn.to_simplified(text) == text

    def test_empty_and_none_like(self):
        assert ztn.to_simplified("") == ""
        assert ztn.to_simplified("   ") == "   "

    def test_conversion_failure_returns_original(self, monkeypatch):
        def _boom():
            raise RuntimeError("opencc missing")

        monkeypatch.setattr(ztn, "_converter", _boom)
        assert ztn.to_simplified("醫學") == "醫學"  # 原样返回，不抛异常


class TestNormalizeAsrResult:
    def test_segments_and_words_converted(self):
        result = {
            "text": "複雜任務",
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.5,
                    "text": "把複雜任務拆成幾個步驟",
                    "words": [
                        {"word": "複雜", "start": 0.0, "end": 0.5, "probability": 0.9},
                        {"word": "任務", "start": 0.5, "end": 1.0, "probability": 0.8},
                    ],
                }
            ],
        }
        out = ztn.normalize_asr_result(result)
        assert out["text"] == "把复杂任务拆成几个步骤"  # text 由规整后分段重建
        assert out["segments"][0]["text"] == "把复杂任务拆成几个步骤"
        assert [w["word"] for w in out["segments"][0]["words"]] == ["复杂", "任务"]
        # 时间戳与词数不动
        assert out["segments"][0]["start"] == 0.0
        assert len(out["segments"][0]["words"]) == 2

    def test_rebuilt_text_from_normalized_segments(self):
        result = {
            "text": "舊全文",
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "醫學", "words": []},
                {"start": 1.0, "end": 2.0, "text": "報告", "words": []},
            ],
        }
        out = ztn.normalize_asr_result(result)
        assert out["text"] == "医学报告"

    def test_no_segments_converts_text_only(self):
        result = {"text": "檢查報告", "segments": None}
        out = ztn.normalize_asr_result(result)
        assert out["text"] == "检查报告"

    def test_non_dict_passthrough(self):
        assert ztn.normalize_asr_result(None) is None
        sentinel: list = []
        assert ztn.normalize_asr_result(sentinel) is sentinel  # type: ignore[arg-type]

    def test_malformed_segment_entries_skipped(self):
        result = {"segments": ["bad", {"start": 0.0, "end": 1.0, "text": "藥"}, 42]}
        out = ztn.normalize_asr_result(result)
        assert out["segments"][1]["text"] == "药"


class TestVideoUtilsWiring:
    def test_transcribe_return_normalized(self, monkeypatch):
        """transcribe_with_faster_whisper 的返回必须经过 t2s 规整。"""
        import contextlib

        import src.video_utils as vu

        # 单测不与生产 ASR 争锁（真实 worker 转写期间会持有 asr.lock）。
        monkeypatch.setattr(
            vu, "process_lock", lambda path: contextlib.nullcontext()
        )

        class _FakeWord:
            def __init__(self, w):
                self.word = w
                self.start = 0.0
                self.end = 0.4
                self.probability = 0.9

        class _FakeSeg:
            start = 0.0
            end = 1.0
            text = "醫學檢查"
            words = [_FakeWord("醫學"), _FakeWord("檢查")]

        class _FakeModel:
            def __init__(self, *a, **k):
                pass

            def transcribe(self, path, **kwargs):
                assert kwargs.get("initial_prompt"), "必须带简体 initial_prompt"
                return [_FakeSeg()], type("Info", (), {"language": "zh"})()

        monkeypatch.setattr(vu, "_FASTER_WHISPER_AVAILABLE", True)
        monkeypatch.setattr(vu, "_FASTER_WHISPER_MODEL_CACHE", {("small", "cpu", "int8"): _FakeModel()})
        monkeypatch.setattr(vu, "_prepare_audio_for_transcription", lambda p: p)

        config = type("C", (), {"faster_whisper_model": "small", "faster_whisper_device": "cpu", "faster_whisper_compute_type": "int8", "faster_whisper_vad_filter": True})()
        out = vu.transcribe_with_faster_whisper(__import__("pathlib").Path("x.mp4"), config)
        assert out["text"] == "医学检查"
        assert [w["word"] for w in out["segments"][0]["words"]] == ["医学", "检查"]
