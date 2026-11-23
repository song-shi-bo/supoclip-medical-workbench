"""局部任务（Local Task）：把「什么都跑 AUTO_EDIT」拆成有类型的、按 clip 收敛的任务。

背景（改造要解决的问题）
------------------------
改造前助手的 `make_cover` / `render_clip` 动作都会走 `rerender_auto_edit_task()`：
新建一个 ``autoedit_xxxx`` 任务 → 新任务有自己的 output 目录 → 前端按任务聚合 clips
→ 左侧多出一张重复的「剪辑成片 01」卡片。文案/字幕/时间线类修改同样被粗暴地当成
「重跑自动剪辑」（3~4 分钟、磁盘增量 ~20MB）。

任务类型（本模块是唯一的事实源）
--------------------------------
======================  ==============  ==================================================
task_type               task_id 前缀   语义
======================  ==============  ==================================================
``AUTO_EDIT``           ``autoedit_``  转写完成后**首次**按既有固化规则生成成片（原有链路）
``REGENERATE_COVER``    ``cover_``     只换当前 clip 的封面图（+ 0.8s 片头），不重剪不重转写
``UPDATE_TITLE``        ``title_``     只更新标题/主题/关键词/封面文字
``UPDATE_SUBTITLE_STYLE`` ``subtitle_`` 只更新字幕样式配置
``UPDATE_TIMELINE``     ``edit_``      只改当前 clip 的时间线片段
``UPDATE_SUBTITLE``     ``msub``       字幕文本/时间/样式 → 写 config+overrides 并产出新 ASS/SRT 版本
``UPDATE_TRANSFORM``    ``xfm``        画面变换「应用」→ 写 clip ``transform`` + 既有 sidecar
``UPDATE_AUDIO``        ``aud``        BGM 音量/淡入出/原声音量/静音
``UPDATE_EFFECTS``      ``fx``         贴纸/片尾/滤镜/转场/导出分辨率
``RENDER_CLIP``         ``rnd``        本阶段只登记待渲染（真渲染属 Phase 2），不 bump 版本号
======================  ==============  ==================================================

Inspector Phase 1（契约 ``docs/inspector-phase1-contract.md`` §4）
----------------------------------------------------------------
后五个类型是右侧 Inspector 的**局部通道**：配置与版本落到位，但**导出侧真渲染属 Phase 2**
（响应里固定带 ``export_*: "phase2"`` / ``render_deferred`` 标注，绝不伪实现）。
它们的共同规则：

* 只改本 clip 的配置/资产（台账条目 + 该 clip 的 ``output/{render_task_id}/{clip_id}/``）；
* **绝不**调用 ``studio_auto_edit_bridge.submit_auto_edit_task`` / ``rerender_auto_edit_task``，
  也绝不创建 ``autoedit_*`` 任务（``FULL_PIPELINE_TYPES`` 仍只有 ``AUTO_EDIT``）；
* 校验复用 ``clip_config``（默认值/范围/枚举/transform 的唯一事实源），字幕合成复用
  ``subtitle_compose``，转写复用 ``transcript_revisions`` —— 本模块只负责编排与落盘。

落盘（刷新后可恢复；路径都在 ``STUDIO_TASKS_ROOT`` 下）
-------------------------------------------------------
- ``localtasks/{task_id}.json``   局部任务记录（含 instruction / payload / result）
- ``status/{task_id}.json``       状态镜像（与 AUTO_EDIT 共用同一张「任务表」）
- ``logs/local_tasks.jsonl``      追加式任务日志（日志可见：只建局部任务，不建 auto_edit）
- ``projects/{project_id}/clips.json``  项目台账（studio_project_store）
- ``output/{render_task_id}/{clip_id}/`` 成片与封面资产本体（路径不变）
- ``output/{render_task_id}/{clip_id}/cover_backup/rev-NNN/`` 封面历史版本（可撤销）

设计约束
--------
- **绝不新建 clip、绝不改 render_task_id**：所有局部任务都作用在台账里已有的 clip 上。
- 封面任务只走 ``cover_replace``（备份 + 片头局部拼接 + 幂等 + 可回滚）。
- 缺权重（``cutout_v3`` 的 u2net）→ 显式失败并带安装指引，不静默换风格。
- 任务在后台线程执行、状态可轮询；失败只影响自己那个字段，台账不会被写坏。
"""

# ---------------------------------------------------------------------------
# 阶段5·点2（2026-11-23）：studio_local_tasks 由单文件拆为本包。
# 本 __init__ 把各子模块的**全部顶层符号（含下划线私有名）**并入包命名空间，
# 保证既有导入面 100% 兼容：`from src.studio_local_tasks import X`、
# `from .studio_local_tasks import X` 与 `import studio_local_tasks; tasks._handle_*`
# 均与拆分前行为一致。子模块职责见各自文件头 docstring。
# ---------------------------------------------------------------------------

from __future__ import annotations

from . import records
from . import context
from . import textutils
from . import lifecycle
from . import covers
from . import handlers
from . import runner


for _mod in (records, context, textutils, lifecycle, covers, handlers, runner):
    for _name in dir(_mod):
        if _name.startswith("__"):
            continue
        globals().setdefault(_name, getattr(_mod, _name))
del _mod, _name


# ---------------------------------------------------------------------------
# 阶段5·点2：对**包命名空间** monkeypatch 的兼容路由（零 body 编辑）。
# 单文件时代测试 setattr(tasks, "replace_cover", fake) 直接命中唯一模块全局；
# 拆包后各子模块持有 `..cover_replace` 的原始绑定——在此把它们替换为
# 「调用时从包 facade 取同名属性」的代理（未 patch 时即原实现）。
# TASKS_ROOT 不走本机制：records._tasks_root() 已在函数体层动态解析。
# ---------------------------------------------------------------------------

class _RoutedCallable:
    __slots__ = ("_name", "_fallback")

    def __init__(self, name: str, fallback) -> None:
        self._name = name
        self._fallback = fallback

    def __call__(self, *args, **kwargs):
        import sys
        # 本类定义在包 __init__：__name__ 就是包名本身（如 src.studio_local_tasks）
        pkg = sys.modules[__name__]
        return getattr(pkg, self._name, self._fallback)(*args, **kwargs)


# 名单 = 测试在包 facade 上 monkeypatch 的全部可替换函数（grep setattr(tasks|slt|local, ...) 全量）
for _routed_name in ("replace_cover", "apply_template_cover", "activate_cover_revision",
                     "new_task_id", "_log_line", "_save"):
    for _mod in (records, context, textutils, lifecycle, covers, handlers, runner):
        _bound = getattr(_mod, _routed_name, None)
        if _bound is not None and not isinstance(_bound, _RoutedCallable):
            setattr(_mod, _routed_name, _RoutedCallable(_routed_name, _bound))
del _routed_name, _mod, _bound

