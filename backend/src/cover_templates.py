"""封面版型库注册表（Cover Template Registry · 2026-10-21 阶段）。

职责：
1. **版本化的版型注册表**：四种封面版型（可爱版 / 酷炫版 / 动漫排版 / 黄色大字）
   各带独立 ``template_id`` 与模板版本，这里是**唯一事实源**——前端版型卡、
   台账选择校验与未来的真实封面渲染都必须从这里取模板；绝不允许把四种版型
   写成散落的四套组件或前端硬编码数组；
2. 只读端点载荷（``GET /studio/cover-templates``）；
3. **模拟结果 stub**（``POST /studio/cover-templates/{template_id}/mock-result``）：
   本阶段不调用模型、不生成真实封面，返回**确定性**的模拟载荷（同 id 同输出）。
   后续接入真实渲染时只替换本模块 stub 的内部实现，路由与载荷形状保持不变。

设计约束：
* 只用标准库；
* 注册表是进程内常量（版型是代码资产，不是用户数据）；``REGISTRY_VERSION``
  在版型集合或示例变化时递增，台账里的选择会记录选取时的注册表版本；
* ``validate_template_id`` 供台账写入侧（studio_project_store）复用；
* 示例图（example_path）由后端路由出图（backend/assets/cover-templates/*.svg，
  GET /studio/cover-templates/{id}/example.svg）——前端 public/ 不经过 ingress，
  3445 入口只路由 /studio 与 /video-api。示例只做版型示意，不是生成的封面。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any

E_TEMPLATE_UNKNOWN = "E_TEMPLATE_UNKNOWN"
E_TEMPLATE_ID_INVALID = "E_TEMPLATE_ID_INVALID"

TEMPLATE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: 注册表版本：版型集合 / 示例 / 元数据变化时递增（语义化：日期.序号）。
#: .2：新增每版型的 machine-readable 版面契约（layout），供真实渲染按版面出图。
#: .3：示例图改由后端路由出图（GET /studio/cover-templates/{id}/example.svg）——
#:     前端 public/ 资产不经过 Caddy ingress（3445 只路由 /studio 与 /video-api）。
#: .1（2026-10-22）：四套版型视觉规则最小修订（用户规范）——酷炫/黄色大字的深色遮罩
#:     从「整幅压暗」改为「只在文字背后的圆角暗底面板」；黄色大字主标题移到中下部、
#:     标签明显小于主标题（新增 hook.start_size）；可爱版标题改深棕配奶油卡片、标签
#:     珊瑚粉；酷炫版收敛为「白 + 青」两种文字色；动漫版主标题改框内深色粗体；
#:     人脸避让区扩到 0.72 覆盖嘴部（渲染层）。
REGISTRY_VERSION = "2026-10-22.1"


class CoverTemplateError(ValueError):
    """带机器码的注册表错误（路由层映射 4xx 并透出可读原因）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CoverTemplate:
    """一种封面版型。``template_id`` 稳定不变；改样式请递增 ``version``。"""

    template_id: str
    name: str           # 展示名（如「可爱版」；生成按钮文案 = 使用{name}制作封面）
    version: str        # 模板自身版本（版型内部样式演进时递增）
    example_path: str   # 前端静态示例图（public/ 下），仅版型示意
    description: str
    sort: int           # 卡片展示顺序（稳定升序）


COVER_TEMPLATES: tuple[CoverTemplate, ...] = (
    CoverTemplate(
        template_id="cute",
        name="可爱版",
        version="3",
        example_path="/studio/cover-templates/cute/example.svg",
        description="圆角气泡与柔和配色，标题短句放大，适合轻松科普话题。",
        sort=1,
    ),
    CoverTemplate(
        template_id="cool",
        name="酷炫版",
        version="3",
        example_path="/studio/cover-templates/cool/example.svg",
        description="深色底+霓虹描边，强调冲击力，适合观点与反差类内容。",
        sort=2,
    ),
    CoverTemplate(
        template_id="anime_layout",
        name="动漫排版",
        version="3",
        example_path="/studio/cover-templates/anime_layout/example.svg",
        description="分格对话式排版与拟声词点缀，节奏感强，适合剧情化剪辑。",
        sort=3,
    ),
    CoverTemplate(
        template_id="yellow_bold",
        name="黄色大字",
        version="3",
        example_path="/studio/cover-templates/yellow_bold/example.svg",
        description="亮黄粗体大字压屏，信息一眼可读，适合知识点罗列。",
        sort=4,
    ),
)

# ---------------------------------------------------------------------------
# 版面契约（machine-readable layout）：真实渲染按契约出图，示例图只是示意。
#
# 字段全部是**确定性参数**（无模型参与），由 cover_realframe_v2 的排版/绘制层
# 以「覆盖层」方式应用（apply_template_layout / draw 模板装饰）：
#   scrim       底部/顶部渐变暗幕（保证压屏文字可读）
#   theme/hook  主标题与主钩子的颜色、描边、起始字号倍率（自动缩宽仍然生效）
#               与垂直锚点（THEME_TOP_RATIO 覆盖）
#   panel       文字块背后的圆角面板（可爱/动漫的「卡片感」）
#   accent_bar  标题下的强调色条（动漫红条）
#   bubbles     固定比例位置的柔光圆（可爱气泡）
# 人脸避让、底部安全区、自动缩宽等既有不变量全部保持。
# ---------------------------------------------------------------------------

_TEMPLATE_LAYOUTS: dict[str, dict[str, Any]] = {
    # 可爱版：暖白/浅奶油圆角卡片 + 珊瑚粉强调；主题标签粉色小字在上方留白区，
    # 主标题深棕粗体收在卡片内；恰好两种小装饰（两枚柔和圆点）。
    "cute": {
        "theme": {"color": "#E64980", "stroke_color": "#FFF8F0", "stroke_width": 3,
                  "start_size": 60, "top_ratio": 0.16},
        "hook": {"color": "#5B4636", "stroke_color": "#FFF8F0", "stroke_width": 3},
        "panel": {"fill": [255, 247, 238], "opacity": 228, "radius": 34, "pad": 34,
                  "outline": {"color": [255, 158, 181], "width": 4}},
        "bubbles": [
            {"cx_ratio": 0.12, "cy_ratio": 0.10, "r_ratio": 0.11, "color": [255, 214, 224], "opacity": 190},
            # 0.80/0.12：柔光圆避开片内烧录字幕带（0.86/0.14 会蹭到字幕顶部）
            {"cx_ratio": 0.88, "cy_ratio": 0.80, "r_ratio": 0.12, "color": [255, 228, 236], "opacity": 170},
        ],
    },
    # 酷炫版：深色遮罩只在文字背后（圆角暗底面板），不再整幅压暗下部；文字色收敛为
    # 「白 + 青」两种（标签青色小字，主标题白色粗体），细线描边 + 窄色条。
    "cool": {
        "theme": {"color": "#22D3EE", "stroke_color": "#0B1220", "stroke_width": 4,
                  "start_size": 60, "top_ratio": 0.22},
        "hook": {"color": "#FFFFFF", "stroke_color": "#0B1220", "stroke_width": 6},
        "panel": {"fill": [8, 12, 22], "opacity": 178, "radius": 18, "pad": 32,
                  "outline": {"color": [34, 211, 238], "width": 2}},
        "accent_bar": {"color": [34, 211, 238], "height": 8, "width_ratio": 0.42},
    },
    # 动漫排版：黑白基础 + 单一红色强调条；主题小框 + 主标题同框（≤2 分区），
    # 主标题改框内深色粗体（白底白字不可读）；不加对白框尾巴/速度线。
    "anime_layout": {
        "theme": {"color": "#111827", "stroke_color": "#FFFFFF", "stroke_width": 4,
                  "start_size": 72, "top_ratio": 0.14},
        "hook": {"color": "#111827", "stroke_color": "#FFFFFF", "stroke_width": 4},
        "panel": {"fill": [255, 255, 255], "opacity": 244, "radius": 10, "pad": 30,
                  "outline": {"color": [17, 24, 39], "width": 6}},
        "accent_bar": {"color": [220, 38, 38], "height": 12, "width_ratio": 0.40},
    },
    # 黄色大字：局部半透明暗底只在文字背后（原中部整幅压暗取消）；主标题区下移到
    # 中下部（0.44）、起始档 112 尽量吃满安全画幅 80%（自动缩宽兜底）；标签 54 明显小。
    "yellow_bold": {
        "theme": {"color": "#FDE047", "stroke_color": "#111827", "stroke_width": 4,
                  "start_size": 54, "top_ratio": 0.44},
        "hook": {"color": "#FDE047", "stroke_color": "#111827", "stroke_width": 10,
                 "start_size": 112},
        "panel": {"fill": [17, 24, 39], "opacity": 150, "radius": 22, "pad": 36},
    },
}


def layout_contract(template_id: str) -> dict[str, Any]:
    """版型的版面契约（未注册版型按未知处理；返回副本，调用方可安全修改）。"""
    template = get_template(template_id)
    if template is None:
        raise CoverTemplateError(E_TEMPLATE_UNKNOWN,
                                 f"未知封面版型 {template_id!r}（注册表 v{REGISTRY_VERSION}）")
    import copy
    return copy.deepcopy(_TEMPLATE_LAYOUTS.get(template.template_id) or {})


def template_render_spec(template_id: str) -> dict[str, Any]:
    """渲染缝入参：template_id + 展示名 + 注册表版本 + 版面契约（一次解析）。"""
    template = get_template(template_id)
    if template is None:
        validate_template_id(template_id)  # 统一抛 E_TEMPLATE_UNKNOWN
    return {
        "template_id": template.template_id,
        "name": template.name,
        "version": template.version,
        "registry_version": REGISTRY_VERSION,
        "layout": layout_contract(template.template_id),
    }

_BY_ID: dict[str, CoverTemplate] = {t.template_id: t for t in COVER_TEMPLATES}


def list_templates() -> dict[str, Any]:
    """GET /studio/cover-templates 载荷（只读；顺序稳定）。"""
    return {
        "registry_version": REGISTRY_VERSION,
        "templates": [asdict(t) for t in sorted(COVER_TEMPLATES, key=lambda t: t.sort)],
    }


def get_template(template_id: str) -> CoverTemplate | None:
    return _BY_ID.get(str(template_id or "").strip())


def validate_template_id(template_id: str) -> str:
    """台账写入侧的模板校验：格式 + 必须已注册（绝不静默接受未知版型）。"""
    value = str(template_id or "").strip()
    if not TEMPLATE_ID_PATTERN.fullmatch(value):
        raise CoverTemplateError(E_TEMPLATE_ID_INVALID, f"非法 template_id: {template_id!r}")
    if value not in _BY_ID:
        raise CoverTemplateError(
            E_TEMPLATE_UNKNOWN,
            f"未知封面版型 {value!r}；可用: {', '.join(sorted(_BY_ID))}（注册表 v{REGISTRY_VERSION}）")
    return value


def mock_result(template_id: str) -> dict[str, Any]:
    """模拟生成结果（确定性；绝不调用模型、不写任何封面文件）。

    ``result_text`` 直接可展示；后续真实渲染接入时保持载荷形状
    （mock 换成 false、result 补充 cover_asset 等字段）。
    """
    template = get_template(template_id)
    if template is None:
        validate_template_id(template_id)  # 统一抛 E_TEMPLATE_UNKNOWN / E_TEMPLATE_ID_INVALID
    return {
        "mock": True,
        "registry_version": REGISTRY_VERSION,
        "template_id": template.template_id,
        "template_name": template.name,
        "template_version": template.version,
        "example_path": template.example_path,
        "result_text": f"已按「{template.name}」完成模拟排版（本阶段为模拟结果，不调用模型、不生成真实封面）。",
    }
