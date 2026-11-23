"""选题卡生成管线（阶段3）定向单测：硬校验器 + prompt 构造，不碰 DB 与真实 LLM。

覆盖：
* ``speakable_char_count``：CJK 按字、ASCII 词串合并计数、空白标点不计；
* ``validate_script_draft``：
  - 干净稿：无 hard；
  - 禁止表达命中（含标点变体规避子串的情况）→ hard；
  - refs 缺失 → hard；
  - 红线模式（具体剂量/自行停药/劝阻就医/绝对化疗效）→ hard；
  - 字数越界 → soft；
  - 必要前提未覆盖 → soft；已覆盖 → 无该 soft；
* ``build_brief_draft_prompt`` / ``build_script_prompt``：关键字段与"禁止复用原句"约束注入。
"""

from __future__ import annotations

from src.topic_pipeline import (
    SEED_NEGATIVE_PATTERNS,
    ShootingScriptDraft,
    build_brief_draft_prompt,
    build_script_prompt,
    speakable_char_count,
    validate_script_draft,
)


def _draft(hook: str = "血压正常就说明高血压治好了吗", body: str | None = None,
           cta: str = "有疑问评论区留言", refs: list[str] | None = None) -> ShootingScriptDraft:
    if body is None:
        # 约 190 个可读字，落在 60 秒建议区间 [180, 252] 内
        body = ("很多患者看到血压降到正常就自己把药停了，结果血压反弹得比之前还高。"
                "要说明的是，血压正常正是药物在起作用，不是疾病被治愈。"
                "高血压目前需要长期管理，任何调整都要由医生根据你的整体情况来决定。"
                "如果服药后出现头晕乏力等不舒服，正确的做法是及时复诊，而不是自己减药。"
                "平时配合低盐饮食、规律作息、适量运动，定期测量血压并记录下来。"
                "把这些讲清楚，能帮很多人避免一次危险的擅自决策。") * 1
    return ShootingScriptDraft(
        title="血压正常能停药吗",
        hook=hook,
        body=body,
        cta=cta,
        shooting_notes="口播，白底，无需道具",
        refs=refs if refs is not None else ["中国高血压防治指南（2018）长期管理原则"],
    )


class TestSpeakableCharCount:
    def test_cjk_per_char(self):
        assert speakable_char_count("高血压") == 3

    def test_ascii_words_merged(self):
        # 每天(2)吃(1)药(1) = 4 个 CJK，"5mg" 记 1
        assert speakable_char_count("每天吃 5mg 药") == 4 + 1

    def test_punct_whitespace_ignored(self):
        assert speakable_char_count("你好，世界！ hello world") == 4 + 2


class TestValidateScriptDraft:
    def test_clean_draft_has_no_hard(self):
        hard, soft = validate_script_draft(_draft(), duration_seconds=60)
        assert hard == []
        assert soft == []

    def test_forbidden_expr_hit_is_hard(self):
        draft = _draft(hook="吃了降压药可以自行停药吗")
        hard, _ = validate_script_draft(
            draft, forbidden_expr=["自行停药"], duration_seconds=60
        )
        assert any("自行停药" in v for v in hard)

    def test_forbidden_expr_with_punct_variant_still_hit(self):
        # 禁止表达带标点、稿件无标点：归一化后仍应命中
        draft = _draft(body="医生说了可以自己停药就万事大吉")
        hard, _ = validate_script_draft(
            draft, forbidden_expr=["可以，自己停药"], duration_seconds=60,
        )
        assert any("禁止表达" in v for v in hard)

    def test_missing_refs_is_hard(self):
        draft = _draft(refs=[])
        hard, _ = validate_script_draft(draft, duration_seconds=60)
        assert any("refs" in v for v in hard)

    def test_redline_dosage_is_hard(self):
        draft = _draft(body="一般每天吃 5mg 就够了，其余和平时一样按时复诊即可，不用特别紧张，"
                            "保持规律作息和低盐饮食，血压自然会平稳，有任何疑问记得来问我。")
        hard, _ = validate_script_draft(draft, duration_seconds=60)
        assert any("红线" in v for v in hard)

    def test_length_out_of_range_is_soft_only(self):
        draft = _draft(body="太短了")
        hard, soft = validate_script_draft(draft, duration_seconds=60)
        assert hard == []
        assert any("字数" in v for v in soft)

    def test_prerequisite_miss_is_soft_covered_is_silent(self):
        hard, soft = validate_script_draft(
            _draft(), prerequisites=["患者已确诊"], duration_seconds=60
        )
        assert hard == []
        assert any("前提" in v for v in soft)
        hard2, soft2 = validate_script_draft(
            _draft(body="如果患者已经确诊高血压并且开始服药，血压正常正是药物控制的结果，"
                        "千万不要自行停药，任何调药都要听医生的，定期复诊监测血压变化，"
                        "配合低盐饮食和规律运动，把血压长期稳定在目标范围内。"),
            prerequisites=["患者已经确诊"], duration_seconds=60,
        )
        assert hard2 == []
        assert not any("前提" in v for v in soft2)


class TestDoctorFeedbackRegression:
    """2026-11-20 医生审稿反馈沉淀：这三类问题句式不允许再漏进成稿。"""

    def test_absolute_blocking_claims_are_hard(self):
        # 医生反馈①：'三次完全可以阻断癌变'/'规范干预就能完全稳住'超出指南口径
        # （反馈模式已 DB 化，这里以种子模式作为 extra_patterns 传入）
        draft = _draft(body="胃炎到胃癌的过程中有三次完全可以阻断癌变的机会，"
                            "只要规范干预就能完全稳住病情，不必过度担心。")
        hard, _ = validate_script_draft(draft, duration_seconds=60,
                                        extra_patterns=SEED_NEGATIVE_PATTERNS)
        assert any("完全" in v for v in hard)

    def test_promissory_patterns_are_soft_warnings(self):
        # 医生反馈②：'就能/也能'句式承诺化 → soft 警告提示软化
        draft = _draft(body="在浅表性胃炎阶段及时处理，规范干预就能控制进展；"
                            "到了萎缩性胃炎阶段，及时处理也能阻断癌变进程，"
                            "这两类情况都需要在正规消化科医生指导下进行调整和治疗，"
                            "同时配合规律作息与均衡饮食，把胃部负担降到比较低的水平，"
                            "并且按医生建议的频率复查，随时掌握自己的恢复情况。")
        hard, soft = validate_script_draft(draft, duration_seconds=60,
                                           extra_patterns=SEED_NEGATIVE_PATTERNS)
        assert hard == []
        assert any("就能" in v for v in soft)
        assert any("也能" in v for v in soft)

    def test_evidence_aligned_phrasing_passes_clean(self):
        # 医生建议的合规口径：'明显降低癌变风险'/'大多能稳住' 不应被误伤
        draft = _draft(body="从胃炎到胃癌，有三次明显降低癌变风险的关键机会。"
                            "浅表性胃炎阶段及时规范干预，病情大多能稳住；"
                            "即使到了癌前病变阶段，遵医嘱处理也有机会中断这条进展链。"
                            "平时注意饮食规律、少吃刺激性的食物，出现长期胃部不适不要硬扛，"
                            "尽早到消化科做系统评估，按医生建议的频率做胃镜复查，"
                            "把小问题在早期就管理起来，避免拖到难以处理的地步。"
                            "把这些习惯坚持下来，就是给胃健康最实在的投入。")
        hard, soft = validate_script_draft(draft, duration_seconds=60,
                                           extra_patterns=SEED_NEGATIVE_PATTERNS)
        assert hard == []
        assert soft == []

    def test_extra_pattern_drives_hard_and_soft(self):
        # 负例库模式随 extra_patterns 生效：hard 入拒绝、soft 入警告
        hard_one = _draft(body="这个方法可以彻底解决你的胃病问题，"
                               "其余内容和平时一样按时复诊即可，有任何疑问欢迎随时留言交流，"
                               "也希望你能把这些习惯分享给身边有需要的人，让更多人受益。")
        hard, soft = validate_script_draft(hard_one, duration_seconds=60,
                                           extra_patterns=[("彻底(治愈|根治|消除|解决)", "绝对化（彻底…）", "hard")])
        assert any("彻底" in v for v in hard)
        soft_one = _draft(body="坚持规律复查，就能控制住大部分风险，"
                               "配合低盐饮食和适度运动，规律作息不熬夜，"
                               "保持心态平稳对胃部健康同样重要，"
                               "有不舒服及时就诊才是最稳妥的做法。")
        _, soft2 = validate_script_draft(soft_one, duration_seconds=60,
                                         extra_patterns=[("就能(控制|阻断|稳住)", "承诺化句式（就能…）", "soft")])
        assert any("就能" in v for v in soft2)


class TestPrompts:
    def test_brief_prompt_wraps_transcript(self):
        prompt = build_brief_draft_prompt("这是一条转写稿内容")
        assert "<transcript>" in prompt and "转写稿" in prompt

    def test_script_prompt_contains_brief_and_no_reuse_rule(self):
        prompt = build_script_prompt(
            brief={"user_question": "血压正常能停药吗", "suggested_duration_seconds": 60},
            core_conclusion="血压正常是药物控制的结果",
            risk_boundary="不推荐具体剂量调整",
            forbidden_expr=["自行停药没问题"],
            prerequisites=["患者已确诊"],
            duration_seconds=60,
            reference_transcript="参考稿内容",
            doctor_notes="称呼患者为您",
        )
        assert "血压正常是药物控制的结果" in prompt
        assert "自行停药没问题" in prompt
        assert "禁止复用原句" in prompt
        assert "参考稿内容" in prompt
        assert "称呼患者为您" in prompt
