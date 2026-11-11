# 候选审核：输出 ReviewReport

1. 锁定 inputs.candidate_sha256，审核仅适用于该候选。读取 technical、inventory、translation、source_frames、candidate_frames 与 limitations。附图顺序先原片、后候选，分别以数组顺序和 time_ms 对照；不能把原片当候选或把不同时间的抽帧视为精确同帧比较。
2. 建立 inventory.texts 与 inventory.speech 的完整ID清单。每个ID都检查当前可用证据，evidence 至少给出一条明确记录：`ID | evidence_type=text/frame/technical | 时间或文本来源 | 结论与边界`。covered_ids 表示这些逐项检查已执行，可以仅基于文本对照；它不表示完整媒体观察覆盖，更不证明声音已经收听。无媒体样本覆盖的项仍检查可用文本，并明确记为未验证呈现。
3. 对待译文字检查译文忠实、目标语自然、数字/条件/品牌一致；对 preserve 项检查计划意图。实际候选抽帧覆盖该项时间时，进一步检查旧字残留、文字缺失、截断、缺字方块、遮挡重要内容、颜色可读性与区域漂移。没有抽中该时段时把该ID加入 limitations，不推断整段效果。
4. 对人声检查译文与源片段的文本语义、计划起止及时间轴信息。当前输入未提供可播放音频，所有发音、自然度、响度、断句、说话人及音乐保留的听觉结论保持未验证。转写、文件存在、音轨存在或技术解码通过都不能替代听验。limitations 明确列出未收听以及受影响ID。
5. 用证据支持 defects：critical 为严重误译、关键内容缺失等阻止交付的问题；major 为明显遮挡、截字、错误数字或语义不完整；minor 为有依据的轻微措辞/排版问题。提供 source_id、可定位时提供 start_ms，并写具体 evidence。尚未检查本身是限制，不凭空编造缺陷。
6. verdict：有明确需要修改的问题用 needs_revision；现有证据无重大问题但存在未播放、未收听、未抽中或用户待确认项，用 needs_human。当前抽帧与文本模式应保留 needs_human，不能给出完整质量 pass。无论模型结论如何，系统仍要求人工完整视听批准当前哈希后交付。
7. 输出 covered_ids（全清单ID且每项一次）、defects、evidence、limitations、verdict。evidence 含候选哈希及逐ID证据；limitations 的每项说明未验证什么、受哪些ID影响、人工需要怎样核实。

完成条件：逐项证据齐全，视觉实际观察与文本对照明确分开，听觉未验证事实完整保留，所有缺陷可定位，审核对象绑定当前候选。
