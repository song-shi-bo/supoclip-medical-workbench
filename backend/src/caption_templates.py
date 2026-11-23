"""兼容垫片（阶段5·点2）：本模块已迁移至 `src/domains/render/caption_templates.py`。

旧导入路径继续有效。本垫片是 impl 模块的**代理**：读属性动态转发到 impl；
对垫片属性的 setattr（测试 monkeypatch 的既有方式）同样落到 impl 模块全局——
impl 内部调用即刻可见，与单文件时代语义完全一致。新代码一律从领域包导入。
"""

import sys as _sys
import types as _types

from src.domains.render import caption_templates as _impl


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
