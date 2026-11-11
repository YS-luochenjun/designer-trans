# 新设备安装

本仓库是项目源码，不含当前维护者的任务、身份或已安装环境。Windows 已有运行基础，其他系统的完整制作流程尚未验收。

## 前置条件与安装

准备 Python 3.12+、Node.js 22+、pnpm 11、FFmpeg 和 ffprobe，确保命令可从 PATH 调用。为依赖、可选模型、任务媒体与临时视频预留独立空间。

在项目根目录执行：

```powershell
python scripts/bootstrap.py --check
python scripts/bootstrap.py --agent codex
```

可选 `--agent harness` 安装另一个项目内 Agent 运行时。安装本身不登录、不调用模型。前端依赖使用锁文件；Python 依赖仍有版本范围，并非跨平台全依赖哈希锁定。

如 Windows 的 pnpm 包装器不能被安装脚本解析，可分步执行：

```powershell
python scripts/setup.py
Push-Location ui
pnpm.cmd install --frozen-lockfile
pnpm.cmd run build
Pop-Location
python scripts/install_codex.py
```

## 配置身份

只在首次安装时复制配置模板：

```powershell
Copy-Item config/application/local.example.json config/application/local.json
```

保留环境变量名和私有文件引用，按所用服务在本机建立私有文件。不要将密钥、Token 或登录目录提交到仓库。安装后需要由设备使用者独立完成所选 Agent 的登录或合法 API 身份配置。

KIE、外部需求系统等能力在模板中默认关闭。凭据存在、本地 doctor 通过与远端调用成功分别成立；启用额外调用前核对账户、模型、费用与任务授权。

保留背景音乐或音效的任务还需音频分离组件。安装工具 `scripts/install_separation.py` 使用固定模型下载与校验流程；下载模型与独立环境不会进入源码仓库。

## 启动与检查

```powershell
.\.venv\Scripts\python.exe -B -m video_translation.interfaces.cli doctor
.\.venv\Scripts\python.exe -B -m video_translation.interfaces.cli serve --port 8767
```

打开 `http://127.0.0.1:8767`。也可使用根目录的 Windows 启动入口。默认服务面向本机，不适合未经额外鉴权与隔离直接暴露公网。

运行会在本机创建 `runtime/`、依赖与缓存。源码导出不包含这些目录；新设备第一次启动没有历史任务。工程可启动与视频完整视听验收仍是独立步骤。
