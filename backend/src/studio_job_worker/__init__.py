"""Studio Job 执行器：认领 R2/本地任务，按任务类型执行并回传产物.

三类任务（config.job_kind 分派，缺省 legacy_full 兼容存量）：
  transcribe_only（TRANSCRIBE_ONLY）
    claim → extracting_audio（准备源片+音轨校验）
          → transcribing（faster-whisper small/CPU/int8/VAD/zh/word_timestamps=True）
          → correcting（DSH 模型全文纠错，普通中文校对；不可用回退保守规整）
          → transcript_ready（终态：纠错转写入版本化存储 + artifact 回传）
    禁止：金句/关键词、剪辑规则、切片规划、渲染、封面、台账写入。
  auto_edit（AUTO_EDIT，前置=已有有效纠错转写）
    config.stop_after="analysis"（第三步，兼容旧 analysis_only=true）：
          claim → retrieving_rules → extracting_highlights（DSH 模型内容分析）
          → analysis_ready（终态：tasks/analysis/<id>.json，不渲染）
    config.stop_after="plan"（第四步默认）：
          claim → retrieving_rules → extracting_highlights（内容分析）
          → planning（完整切片规划：连续区间/柔性时长/warning 不拦截）
          → plan_ready（终态：tasks/plans/<id>/clip_plan.json，不渲染）
    完整链（无停住配置）：claim → retrieving_rules（读取规则/冻结快照）→ extracting_highlights（金句/关键词）
          → editing（时间线工程）→ rendering（video-brain，transcript_pre_corrected 跳过重复纠错）
          → uploading → completed / failed
  legacy_full（存量全链，行为与拆分前一致）
    claim → downloading → transcribing → correcting → analyzing → editing
          → rendering → uploading → completed / failed

运行前提：与后端共用 backend/.env（R2_*/WORKER_SHARED_SECRET/BACKEND_INTERNAL_URL/STUDIO_TASKS_ROOT）。
"""



# ---------------------------------------------------------------------------
# 阶段5·点2（2026-11-23）：studio_job_worker 由单文件拆为本包。
# __init__ 把各子模块的**全部顶层符号（含下划线私有名）**并入包命名空间，
# `python -m src.studio_job_worker`（supervisor 启动方式）由 __main__.py 承接，
# 行为与拆分前一致。子模块职责见各自文件头 docstring。
# ---------------------------------------------------------------------------

from __future__ import annotations

from . import config
from . import reporting
from . import staging
from . import stages
from . import pipeline
from . import runner


for _mod in (config, reporting, staging, stages, pipeline, runner):
    for _name in dir(_mod):
        if _name.startswith("__"):
            continue
        globals().setdefault(_name, getattr(_mod, _name))
del _mod, _name


class _RoutedCallable:
    """facade 同名属性在**调用时**解析（兼容测试对包命名空间的 monkeypatch）。

    关键细节：stages 的 `_accepts_keyword` 用 `inspect.signature` 探测桩函数的
    旧式签名（阶段兼容契约）——代理必须把 `__signature__` 透传到**当前目标**，
    否则所有桩都会被误判为接受 **kwargs，破坏旧签名桩的调用约定。
    """
    __slots__ = ("_name", "_fallback")

    def __init__(self, name, fallback):
        self._name = name
        self._fallback = fallback

    def _target(self):
        import sys
        pkg = sys.modules[__name__]
        return getattr(pkg, self._name, self._fallback)

    def __call__(self, *args, **kwargs):
        return self._target()(*args, **kwargs)

    @property
    def __signature__(self):
        import inspect
        try:
            return inspect.signature(self._target())
        except (TypeError, ValueError):
            return inspect.signature(lambda *args, **kwargs: None)


# 名单 = 测试在包 facade 上 monkeypatch 的全部可替换函数（grep setattr(worker|w, ...) 全量）
# 路由规则：包内定义的**全部函数**在子模块命名空间中的绑定统一替换为
# 「调用时从 facade 取同名属性」的代理——测试 setattr(包, 任意函数名, 桩) 即刻生效，
# 无需维护名单（变量型注入点走各自访问器：BASE_DIR/_CURRENT_JOB/_COVER_LLM_FN）。
import types as _types

for _mod in (config, reporting, staging, stages, pipeline, runner):
    for _name, _bound in list(vars(_mod).items()):
        if _name.startswith("__") or isinstance(_bound, _RoutedCallable):
            continue
        if isinstance(_bound, _types.FunctionType):
            setattr(_mod, _name, _RoutedCallable(_name, _bound))
del _mod, _name, _bound
