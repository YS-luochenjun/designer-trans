# 制作规划

先按 output_schema 识别合同。

## AdaptivePlan（新自主制作）

对照inventory、source_design、translation与实际源图，生成lettering与speakers。

- lettering必须逐项精确覆盖treatment=translate的源ID。样式以保存的源设计为依据，允许为目标语言长度和字形适配字号/描边/换行。text不在本合同里改写，执行器从已定稿译文及最终口播读取。
- font_id只能来自inputs.fonts。font_size、描边、阴影均为源分辨率像素。region是原片完整运动包络；motion.time_ms是源绝对时间，x/y是相对该region左上角的像素平移，scale/opacity线性插值。不要把归一化位置误作像素偏移。没有可信运动证据不能伪造复杂轨迹。
- 普通真实字体优先；需要艺术字且image_artwork_available=true时可选artwork，给出源风格与透明字层提示，不能加入新背景。生成艺术字必须后续逐字核对，不认为一次生成就成功。
- 画面修复在共享clean阶段执行，不在本计划里再次生成视频。复杂遮挡只用已记录并可核验的遮挡关键帧；能力不足须保留缺口。
- speakers必须覆盖实际每个角色，每个speaker恰好一次，voice从inputs.voices选。选择理由关联角色表现；不能承诺克隆原声。同角色跨句保持声线。配音时长不合适时允许后续局部改写，但语义、事实与口播字幕必须同步。
- preserve_music遵守任务保留背景策略，不以改false绕过分离失败。转写的model_estimate仍是待核实时间，不能当成已做词级强制对齐。

## ProductionPlan（历史冻结任务）

只为待译画面文字生成visual_steps，source_id完整且唯一。route只能取inputs.available_routes。solid_overlay只适用于固定平面/足够留白，明确其不等于自然补底。历史wan_video_edit只支持2–10秒整片合同，不能在该旧任务里改成未冻结的局部分镜方案。voice在旧合同仅是意图，真实音色由旧模型快照控制。所有历史候选仍需审核。
