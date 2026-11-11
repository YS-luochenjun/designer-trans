# 视频文字移除：方法调研与本项目取舍

核验日期：2026-11-11。依据官方仓库的固定提交与实际推理入口阅读，不安装模型、不下载权重、不以当前生产素材运行外部试验。以下是方法及集成建议，不是本项目新增模型的实测结果。

## 共同结论

视频文字清底与带掩码的视频修复有共同的技术基础：确定需要替换的像素，利用可见背景、邻帧信息或模型预测补齐，再只把许可区域贴回原片。它们仍处理每一帧，但不需要让通用 Agent 为每帧独立规划、调用和验收。[1][3][5][7]

“切成镜头”只是限制错误传播的一种手段。更适合本项目的设计是分开文字问题范围、算法窗口和媒体存储单位：以文字连续性组织制作，在运动与遮挡处自适应拆分，算法使用必要重叠上下文，媒体保留局部补片。此项为本项目设计判断，并非已完成的统一最优算法。

## 官方实现对照

| 方法 | 实际处理方式 | 可借鉴内容 | 集成限制 |
| --- | --- | --- | --- |
| ProPainter | 光流补全、图像传播，再进行特征传播与 Transformer 修复；推理使用局部邻帧、参考帧与子视频窗口。[1] | 连续处理、窗口上下文、控制邻帧和参考数量 | 仍有显存与内存开销；代码及模型声明非商业用途，商业应用需另获许可。[2] |
| STTN | 空间与时间注意力联合填充；官方推理选邻帧和跨区间参考。[3] | 一次推理覆盖多帧，利用时序信息 | 原始演示脚本固定缩放到 432×240，并使用默认输出帧率，不能原样作为本项目原生时钟与画质合同。[3] |
| E2FGVI / HQ | 光流引导的视频修复，按邻帧与参考帧处理。[4][5] | 时序背景利用；区分模型版本和分辨率能力 | 基础版固定缩放到 432×240，HQ 保留输入分辨率；项目声明非商业用途。[4][6] |
| video-subtitle-remover | 组合字幕检测、区域掩码、连续范围及不同修复引擎；ProPainter 路线按场景边界再切分并批处理。[7] | 先检测与组织范围，再调用执行器；支持引擎切换 | 应用壳的 Apache-2.0 许可不覆盖所有模型的独立许可，也不证明其路线在本项目素材或设备上最优。[2][6][8] |

STTN 仓库软件许可证为 MIT，但本次未取得足以将所有外链权重、训练数据及第三方组件一并确认适用于商业分发的证据；应按具体权重来源继续核对，不能仅依据顶层 LICENSE 推广授权范围。[9]

本次没有复制或分发上述实现和权重。广告素材等商业场景不能因某个应用壳可公开下载就默认启用其所有内置模型。

## 为什么换一个时序模型仍可能很慢

ProPainter 官方文档提供 `neighbor_length`、`ref_stride`、`resize_ratio`、`fp16`、`subvideo_length` 等资源控制，并提醒视频修复的显存成本。[2] 缩小窗口会影响可见上下文，整片缩小会影响小字与纹理；这些选项需要和质量共同验证。

原始脚本还会读取整段帧并生成演示视频，减少 GPU 子窗口不代表系统内存、解码、图片落盘和留存总量都被限制。[1] 因而本项目适配器应明确输入区间、上下文、内存与临时空间预算，仅返回需要的修复像素及覆盖映射；不能把第三方演示目录整体纳入每轮产物。

不把官方特定显卡、特定分辨率的速度数字当作本设备的预计耗时。实际硬件后端、驱动、可用显存、模型许可与困难片段未核验前，不能承诺安装 ProPainter 或其他模型就会加速。

## 对本项目的建议顺序

1. **先减少重复工作。** 合并同一文字问题的计划与审查；复用已验证原片、底片、轨迹、供体及有效观察，记录实际失效范围。此步骤可先使用已有工具。
2. **按证据选方法。** 有无字来源时核验复用；稳定简单背景可用既有覆盖或供体；运动背景先验证跟踪与配准，仍不可靠的范围再进入受限时序修复候选。
3. **以变化点调节范围。** 剪辑、遮挡、背景显露、文字进出和资源上限决定拆分；同镜头不强行同方法，固定一秒或固定帧数也不作为默认正确答案。
4. **隔离评估后再集成。** 固定来源、掩码、质量要求和成本口径，对比当前方法、旧分段、自适应范围及候选模型；记录失败，不只挑成功片段。
5. **分离制作和留存。** 源媒体加局部补片作为活动表示，必要时导出检查点；清理另用依赖图与授权执行器，保留未决调用、费用和质量异议依据。

详细合同、管家提示词及开发验收见[迭代方案](iteration-plan.md)。本次调研没有实现新的时序引擎、活动任务历史媒体回收或首页管家维护工具。

## 核验来源

以下链接固定到本次阅读的提交；日后采用时应重新核对上游变化和具体资源许可。

1. [ProPainter 推理入口：传播、邻帧和子视频处理](https://github.com/sczhou/ProPainter/blob/326782529a63d997cab80ab21439b92ed422b3d3/inference_propainter.py)。
2. [ProPainter 官方说明：资源控制及代码、模型许可](https://github.com/sczhou/ProPainter/blob/326782529a63d997cab80ab21439b92ed422b3d3/README.md)；[S-Lab License](https://github.com/sczhou/ProPainter/blob/326782529a63d997cab80ab21439b92ed422b3d3/LICENSE)。
3. [STTN 方法说明](https://github.com/researchmm/STTN/blob/f39f62c5bbbe3e3eba084c487353a2c651bfdcde/README.md)；[推理入口](https://github.com/researchmm/STTN/blob/f39f62c5bbbe3e3eba084c487353a2c651bfdcde/test.py)。
4. [E2FGVI 官方说明：基础版与 HQ 的分辨率区别](https://github.com/MCG-NKU/E2FGVI/blob/709cbe319edc21b8a365a28e14cba595a93d62cf/README.md)。
5. [E2FGVI 推理入口](https://github.com/MCG-NKU/E2FGVI/blob/709cbe319edc21b8a365a28e14cba595a93d62cf/test.py)。
6. [E2FGVI 项目许可](https://github.com/MCG-NKU/E2FGVI/blob/709cbe319edc21b8a365a28e14cba595a93d62cf/LICENSE)。
7. [video-subtitle-remover 执行入口：字幕范围、场景边界和批处理](https://github.com/YaoFANGUK/video-subtitle-remover/blob/e109b9ddc1d0e8f153199dfa05c1d767546906d8/backend/main.py)；[配置入口](https://github.com/YaoFANGUK/video-subtitle-remover/blob/e109b9ddc1d0e8f153199dfa05c1d767546906d8/backend/config.py)。
8. [video-subtitle-remover 应用许可证](https://github.com/YaoFANGUK/video-subtitle-remover/blob/e109b9ddc1d0e8f153199dfa05c1d767546906d8/LICENSE)。
9. [STTN 软件许可证](https://github.com/researchmm/STTN/blob/f39f62c5bbbe3e3eba084c487353a2c651bfdcde/LICENSE)。
