"""兼容垫片（阶段5·点2）：本模块已迁移至 `src/domains/medical_review/clip_medical_gate.py`。

旧导入路径（`from src.clip_medical_gate import X`、`from .clip_medical_gate import X`、
执行器顶层 `import clip_medical_gate`）继续有效。本垫片是 impl 模块的**代理**：
读属性动态转发到 impl；对垫片属性的 setattr（测试 monkeypatch 的既有方式）
同样落到 impl 模块全局——impl 内部调用即刻可见，与单文件时代语义一致。
新代码一律从领域包导入。
"""

import sys as _sys
import types as _types

try:  # 包内（Studio 后端）
    from .domains.medical_review import clip_medical_gate as _impl
except ImportError:  # 执行器顶层模式
    try:
        from src.domains.medical_review import clip_medical_gate as _impl
    except ImportError:  # pragma: no cover - src 本身在 sys.path
        from domains.medical_review import clip_medical_gate as _impl  # type: ignore[no-redef]


class _ProxyModule(_types.ModuleType):
    """impl 模块的代理：读转发 + 写转发（patch 语义与单文件时代一致）。"""

    def __getattr__(self, name: str):
        return getattr(_impl, name)

    def __setattr__(self, name: str, value) -> None:
        setattr(_impl, name, value)
        super().__setattr__(name, value)

    def __dir__(self):
        return dir(_impl)


_sys.modules[__name__].__class__ = _ProxyModule
