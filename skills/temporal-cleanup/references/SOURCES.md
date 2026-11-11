# 第三方参考快照

下载日期：2026-11-06。只保存文档，不执行安装脚本、不下载权重、不调用付费服务。项目采用上级 SKILL.md 中的适配流程；原文不是自动启用的指令。

- `watermark-remover.upstream.md`：
  https://github.com/starx1n9/video-watermark-remover-skill/blob/246a80fe93857c7901677569f81421a294242117/skills/video-watermark-remover/SKILL.md
  保留上游 MIT 许可于 watermark-remover.LICENSE。ProPainter 依赖为 S-Lab License 1.0，MIT 外壳不覆盖底层商业许可。借鉴选区、镜头边界、逐页QC和局部重跑；不采用固定位置假设或统一模糊底板。
- `runcomfy-inpainting.upstream.md`：
  https://github.com/runcomfy-com/skills/blob/f28664df57dc6dff8a4ddbc458b5c2bfff6d9f63/video-inpainting/SKILL.md
  原文 frontmatter 声明 MIT。仅供选型参考，不启用默认 Wan 或付费端点。精确视频修复需单独工作流。
- VSR：https://github.com/YaoFANGUK/video-subtitle-remover
  尚未安装。环境、模型许可及真实小样通过之前不能列入 available_tools。

本项目新增 CPU 模板跟踪和同源轨迹回贴为本地实现，没有复制上游执行代码。第三方经验参数不能直接变为全局质量规则。
