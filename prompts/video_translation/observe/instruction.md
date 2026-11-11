# 原片观察

对照实际附图、媒体信息和转写，建立Inventory；若output_schema为ObservationDecision，则通过inspect/finish/clarify逐轮完成清单。

1. 将转写segment映射成稳定speech ID，保留语义和源时段。ASR是待核实的供应商结果；不能凭画面捏造旁白、说话人或词级时间。重叠对白和数字/人名差异须有明确证据处理。
2. 对真实图像中的文字记录原文、源绝对起止时间、归一化region、translate/preserve及理由。region应包含整段文字运动包络，不能只框首帧。内容改变另建ID，连续相同文字按一个实例处理。图案、人物、图标不是文字；需要保留的Logo等明确preserve。
3. 不知道文字出现/消失的边界、细字内容或轨迹时，在自适应流程用inspect请求对应时间附近的帧，逐次缩小区间。已给帧很稀疏时不能把中间全部视为已经查看。最多使用项目提供的轮数/费用，未解决的具体问题进入clarify，避免伪造确定性。
4. Inventory中的所有时段须在duration_ms内，IDs全局唯一。未知语义/要求优先参考已确认source_language、user_answer；无法从原片自行解决才请求澄清。
5. 图像和转写通常不能判定音乐。若无可信音频分析，has_music=false仅为合同占位，在evidence明确music_presence_unverified，不能宣称无音乐，也不要仅因此询问用户。实际保留背景由任务audio_strategy约束，后续分离/混音保留背景。没有音轨则speech为空。
6. evidence逐项引用实际图像时间或ASR片段，并说明未完整播放/收听的边界。影响内容制作的未解决事项放unknowns。finish时unknowns应为空；clarify须指出不能自行解决的具体缺口。

历史Inventory直出合同没有inspect动作时按已有附图和同样证据边界输出清单，不编造已检查内容。
