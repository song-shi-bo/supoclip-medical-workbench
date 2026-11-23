"""领域分包（阶段5·点2，2026-11-23）：按业务域归组模块，为将来按域拆分服务保留边界。

每个领域包内模块保持自包含；旧路径的同名文件是兼容垫片（copy-all 符号），
新代码一律从领域包导入。当前领域（阶段5·点2 域归位完成，2026-11-23）：
- medical_review：医学闸门/指南库/词典
- transcript：转写存储/纠错/修订/说话人标注
- analysis：内容分析/标签/金句/知识上下文/知识嵌入
- planning：切片规划/层级规划/选题管线
- refinement：精修/净化/重编替换
- publish：投递/敏感词替换
- render：渲染/字幕/封面/重构图/视频工具

**job-orchestrator 边界决策**：`studio_jobs`/worker/局部任务/supervisor/HTTP 路由等
应用核心基础设施**留在 src 根**（studio_job_store、studio_job_worker/、studio_local_tasks/、
studio_supervisor、studio_workflow、studio_runtime、api/、services/、main_refactored）——
它们是编排「各领域」的应用核心而非一个业务域，域归位以业务模块为界。
analytics 域待阶段5·点5（数据驱动排序）落地时创建。
"""
