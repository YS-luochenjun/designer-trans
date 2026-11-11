# 视频翻译工作台

项目自身运行的视频本地化工作台。后端管理任务、资源快照、Agent 决策和媒体执行，前端提供制作进度、问题检查与候选审核。项目处于开发验证阶段；工具执行成功和局部修复接受不代表完整视频已通过人工视听验收。

## 能力

- 上传原片，或在配置合法身份后读取外部需求与原片，创建语言版本。
- 按工作流完成内容观察、译文、制作计划、清底、文字合成、声音处理与质量检查。
- Work Agent 决定有限的局部制作动作；Route 处理经授权的技术诊断与恢复，保留未知结果和费用。
- 统一媒体执行层复用抽帧证据，以局部无损补片保存修改，在需要可播放视频时生成并核验完整表示。
- 冻结任务使用的模型、资源和执行参数，保留候选、接受、拒绝及原操作回执。
- 提供接单策略、版本绑定的定向返工、显式预览及受限缓存保留接口；相关策略与额外权限需要单独配置。

## 安装与启动

准备 Python 3.12+、Node.js 22+、pnpm 11、FFmpeg 和 ffprobe。在项目根目录执行：

```powershell
python scripts/bootstrap.py --check
python scripts/bootstrap.py --agent codex
Copy-Item config/application/local.example.json config/application/local.json
```

只在首次配置时复制模板；已有本机配置应合并修改。按需要填写环境设置和私有凭据引用，并在本机完成所选 Agent 的登录。安装器不自动登录，也不发送模型推理请求。

```powershell
.\.venv\Scripts\python.exe -B -m video_translation.interfaces.cli doctor
.\.venv\Scripts\python.exe -B -m video_translation.interfaces.cli serve --port 8767
```

打开 `http://127.0.0.1:8767`，Windows 也可使用根目录的 `启动工作台.cmd`。默认服务面向本机，不是已具备多用户鉴权的公网部署。其他系统的完整安装与制作流程尚未验收。

详细说明见[新设备安装](docs/operations/clone-install.md)。

## 项目结构

| 目录 | 内容 |
| --- | --- |
| `src/video_translation` | 领域合同、应用服务、持久层、Worker、媒体执行及 HTTP 接口 |
| `ui` | React / TypeScript 工作台源码和前端依赖锁文件 |
| `config` | 公开配置模板、模型目录与环境映射 |
| `contracts`、`workflows` | 数据合同和节点依赖 |
| `prompts`、`knowledge`、`skills` | 项目运行所需的提示、知识与技能资源 |
| `scripts` | 安装、Agent 桥接、配置和源码导出工具 |
| `docs` | 项目架构、安装、存储与发布说明 |

参考[架构与边界](docs/architecture.md)、[存储与执行效率](docs/storage-and-performance.md)、[时序清底方法调研](docs/temporal-removal-research.md)、[连续片段与管家迭代方案](docs/iteration-plan.md)、[源码发布](docs/source-release.md)。

## 分发边界

本仓库仅包含整理后的项目源码。生产媒体、任务与数据库、模型权重、登录状态、私有配置、已安装依赖、前端构建结果、测试目录及本机测试排障记录均不包含在内。`SOURCE-MANIFEST.json` 记录本次源码的逐文件大小与 SHA-256。

原开发目录及其完整测试保留在维护者本机。本次发布没有宣称在全新设备上完成真实制作验收，也没有携带任何用户的制作任务。未声明整体项目的开源许可证；引用资源的来源与许可说明保留在对应资源目录。
