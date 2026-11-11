---
name: video-watermark-remover
description: Remove fixed logos / corner bugs / watermarks / burned-in subtitles from videos locally with ProPainter video inpainting. Auto-locates the element and when it appears, shows a mask preview for the user to confirm, inpaints, runs QC, and delivers the clean video + before/after images + a coordinates doc for placing a new logo/subtitles. 本地视频去 logo / 去角标 / 去台标 / 去水印 / 去字幕。触发：「去掉视频里的 logo/角标/台标/水印」「去字幕」「把片头片尾的商标去掉」「remove watermark/logo/subtitles from video」「video watermark remover」「hardsub remover」。
---

# 视频去 logo / 去字幕（video-watermark-remover）

用 ProPainter 修补视频里**位置固定**的 logo、角标和字幕（压在画面上的）。只裁出元素周围的 ROI 去修补，再羽化贴回原片，ROI 以外的像素不动。

> ⚠ ProPainter 的许可证是 S-Lab License 1.0：**只允许非商业用途**，商用要联系原作者授权。
> 只用于你有权处理的素材。

## 0. 环境（首次使用）
脚本都在 `scripts/` 下，**工作目录要设为本 skill 目录**（即 SKILL.md 所在目录）。
```bash
python scripts/setup_env.py      # 检查 ffmpeg / PyTorch(CUDA)，clone 固定版本的 ProPainter，生成 config.json
python scripts/selftest.py       # 合成测试视频跑完整流程，和真值比对，输出 PASS/FAIL
```
- `config.json` 不存在时，`setup_env.py` 会提示缺什么、怎么装。PyTorch 需要用户按自己的显卡安装 CUDA 版。
- 自检不通过不要继续，先按提示修好环境。
- 下文的 `python` 指 `setup_env.py` 写进 config 的那个解释器（也就是跑 setup 时用的环境）。

## 用户要提供什么
| 项 | 必须 | 说明 |
|---|---|---|
| 视频文件 | ✅ | 一段或多段 |
| 要去掉什么 | ✅ | 大白话即可："左上角台标""中间大 logo""底部字幕" |
| 位置提示 | 可选 | 同类元素不止一个时才需要 |
| 时间段 | 可选 | 默认自动检测，只有要**保留一部分**时才需要 |
| 后续打算 | 建议问 | 是否叠新 logo、重配字幕、新字幕有没有底条。有底条时直接打模糊即可，不必修补 |

缺必须项就问用户；可选项不要追问。

## 能力边界（开工前对照，超出的要先告诉用户）
- ✅ 固定位置的 logo、角标、台标，包括半透明的、淡入淡出的、在片头片尾转场里被拉糊的。
- ✅ 固定位置、水平居中、单行的字幕（压在画面上的），白字或深色字，带不带阴影都行。
- ⚠️ 会移动或缩放的 logo、滚动字幕、多行字幕、不居中的字幕：脚本不能自动跟踪，需要用户指出时间段，再用 `--block`、`--grow-from` 手工处理，效果会打折扣。
- ⚠️ 画面里本来就有的文字（标牌、屏幕上的字），和要去掉的元素重叠时会被一起抹糊。要在识别清单里提醒用户。
- 需要 NVIDIA GPU，显存最好空闲 4GB 以上。只用 CPU 也能跑，但非常慢。

## 流程（8 步，按顺序走）
中间文件放在临时工作目录 `$W`。成片放在原视频旁边，质检图放在 `<视频目录>/qc/`。

### 1. 探测
```bash
python scripts/probe.py IN1.mp4 [IN2.mp4] --out-dir $W
```
看抽帧拼图，判断这几件事：
- 元素在哪；
- 是角标还是居中的大 logo；
- 有没有淡入（记下 logo 稳定显示的帧区间）、黑场、缩放或模糊转场；
- 有没有字幕。

### 2. 定位（静态 logo）
```bash
python scripts/locate_static.py --video IN1.mp4@稳定起帧:止帧 [--video IN2.mp4@a:b] --out-dir $W
```
- 起止帧要避开淡入、黑场和转场。
- **多段素材里有同一个 logo 时，一起传进去。** 脚本只取"每段都静止"的像素，这样可以排除背景里碰巧静止的块。
- 看 `static_rel.png`（越黑越静止），据此定 logo 的大框 `--box` 和阈值：实心 logo 用 0.3–0.5，半透明 logo 用 0.6–0.75。

### 3. 做蒙版，并且**必须**让用户确认
**静态 logo：**
```bash
python scripts/mask_logo.py --rel $W/static_rel.npy --box x0,y0,x1,y1 --thresh 0.5 \
    [--block 矩形] [--clear 区域 --block 区域] --preview IN1.mp4@帧 [--preview IN2.mp4@帧] --out-dir $W
```
- **细的元素（文字、射线、线条）按笔画抠**，笔画之间的真实背景留给模型做参考。
- 只有两种情况用 `--block` 矩形：一是实心大块；二是细到抠不全的小字，比如一行很小的英文。
- ❌ **不要把细元素整块盖住**。镜头慢慢推进时，被盖住的那块几乎没有真实背景露出来，模型只能凭空生成，会补出"鳞片纹理"。
- 检查预览图上元素的边缘和尾巴有没有漏。漏掉的部分会被修补时往里带，留下一截彩线。

**字幕：**
```bash
python scripts/mask_subtitle.py IN.mp4 --out-dir $W [--rows y0,y1] [--dark] [--shadow 0,0]
```
- 不给 `--rows` 时，会在画面下 40% 的范围里自动找字幕行；找错了就手动指定。
- 留意输出里"贴近画面边缘"的警告。

**给用户确认**：把 `mask_preview.png` 或 `subtitle_mask_preview.png` 展示给用户，再附一份识别清单。坐标用 `mask_logo.py` 打印出来的值，例如：
> ① 左上角台标：修补框 (21,15)–(124,50)，全程出现
> ② 底部字幕：y485–524，共 47 句
> ③ 1:34 处画面里的一块牌子落在修补区内，会被一起抹糊

**用户确认后才进入第 5 步。**

### 4. 分段
```bash
python scripts/shots.py IN.mp4 --out $W/bounds.json [--extra 178]
```
硬切和淡入淡出转场都会被切开。**不分段就跑，静止镜头里会冒出邻近镜头颜色的"药丸状色块"。**

### 5+6. 修补 + 合成
长视频要放后台运行，定时查看日志并主动汇报进度。
```bash
python scripts/inpaint.py --video IN.mp4 (--mask $W/mask.npy | --masks $W/masks.npz) --bounds $W/bounds.json \
    --workdir $W/run --out OUT.mp4 [--mask-dilation 2] [--grow-from 178:14] [--workers 2]
```
- `--mask-dilation`：默认 2；半透明、带光晕的 logo 用 4。
- `--grow-from 帧:像素`：从某一帧起把蒙版再外扩若干像素，用来盖住缩放、模糊转场里被拉散的 logo。
- **同一个视频既要去 logo 又要去字幕，分两次跑**：第二次的 `--video` 用第一次的输出，不要把两块 ROI 合并成一个大框。
- 中断后用原命令重跑即可，已经完成的段会跳过。
- 输出文件命名为 `原名-nologo.mp4` 或 `原名-nologo-nosub.mp4`。脚本拒绝覆盖原片。
- 耗时参考（RTX 3060，2 路并行）：960×540 的 126 秒视频，左上角标约 17 分钟，字幕带约 21 分钟；5 秒的居中大 logo 约 1 分钟。

### 7. 质检
```bash
python scripts/qc.py --orig IN.mp4 --out OUT.mp4 (--mask .. | --masks ..) --bounds $W/bounds.json --workdir $W/run \
    [--residue blue] [--times 3,12,20] --out-dir <视频目录>/qc/xxx
```
**数值指标：**
- 残影：成片方差应约等于外圈方差。
- 闪烁：比值中位数在 1 左右或更小，突跳帧很少。
- 接缝：比值约 1。
- 区外误差：小于 2。
- 残留色：降到接近 0。
- 全黑帧：0 帧。

**⚠ 数值全部达标也可能有局部问题，必须逐页打开 `sheet_XX.png` 用肉眼看**（色块、鳞片纹理、残字、彩线）。发现问题，回到对应步骤修：
- 色块：用 `shots.py --extra` 补切点。
- 鳞片纹理：改成按笔画抠。
- 残线：补 `--block`。

修完后只重跑受影响的部分，再质检一遍。

### 8. 交付
- 给出成片和对比图的**绝对路径**，并把 `before_after.png` 展示给用户。
- 多段需要拼接时用 `python scripts/concat.py A.mp4 B.mp4 C.mp4 --out FULL.mp4`，脚本会校验总帧数等于各段之和。
- **写坐标文档**，模板见 `references/positions-template.md`。每个元素写三层框：原元素范围、实际修补范围、含羽化的最外圈（即建议新 logo 覆盖的范围）。字幕用 `subtitle_info.json` 里的 `union_box`。
- 已知瑕疵按时间点列清楚，不要藏。

## 硬规则（都是实测踩出来的）
1. **ROI 的宽和高都必须 ≥160。** 高 96 时，ProPainter 在蒙版区输出全黑，换 fp32 也一样。`inpaint.py` 自动选 ROI 时已经保证这一点。
2. **转场必须切开。** 只按硬切分段会漏掉淡入淡出，ProPainter 会跨镜头借像素，出现色块。扩大蒙版或改用 fp32 都解决不了。
3. **细元素按笔画抠，不要整块盖。**
4. **字幕要做居中对称裁剪，同一句字幕用同一个蒙版。** 不做对称裁剪会把背景亮条抠进来；同一句换不同蒙版会闪烁，淡入时也会漏抠。
5. **数值指标会漏掉问题，必须逐页用肉眼看联系图。**
6. **动手修补前必须让用户确认蒙版。** 不覆盖原片，不删除用户的文件。
7. 后台子进程不弹窗口（脚本已内置）。长任务放后台跑，并主动汇报进度。
