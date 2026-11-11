"""Read-only implementation map for the developer architecture view."""
from copy import deepcopy


def node(key, label, kind, detail, *sources):
    return {"id": key, "label": label, "kind": kind, "detail": detail, "sources": list(sources)}


def view(key, title, nodes, edges):
    return {"id": key, "title": title, "nodes": nodes,
            "edges": [{"source": source, "target": target, "label": label}
                      for source, target, label in edges]}


MODULES = [
    {"name": "首页", "responsibility": "项目对话、运行摘要与管理决定；任务问题详情回到对应任务。", "href": "#overview"},
    {"name": "需求中心", "responsibility": "Fireplay 连接、读取需求、核对接单和需求检查历史。", "href": "#fireplay"},
    {"name": "任务中心", "responsibility": "单次制作、节点协作、当前巡检分析、任务控制、额度与候选验收。", "href": "#tasks"},
    {"name": "自动化", "responsibility": "轮询计划、触发结果和后台巡检记录；不重复管理决定表单。", "href": "#schedules"},
    {"name": "成品库", "responsibility": "查找已交付成品与下载；制作验收在任务中心。", "href": "#deliveries"},
    {"name": "设置", "responsibility": "引擎、模型与价格、资源及项目管家执行策略。", "href": "#resources"},
    {"name": "开发者视图", "responsibility": "项目架构、Agent 分工、会话说明与管理执行审计。",
     "href": "#resources/developer"},
]

ROLES = [
    node("manager", "项目管理 Agent", "agent",
         "按事件检查项目快照，分析异常并提出暂停、恢复、重试或刷新建议。"
         "日调用上限独立于制作额度。默认确认后由程序执行；自动暂停需明确启用。",
         "src/video_translation/application/management.py"),
    node("coordinator", "节点协调 Agent", "agent",
         "处理任务节点内的用户消息，读取该节点上下文并提出协作操作；由协作服务校验后应用。",
         "src/video_translation/execution/coordination.py"),
    node("route", "局部 Route Agent", "agent",
         "在原字清理中按证据选择已实现的方法和能力，生成有边界的工作单。"
         "只负责局部路线，不能绕过额度、未知请求核对或质量验收。",
         "src/video_translation/execution/clean_routing.py"),
    node("work", "制作判断 Agent", "agent",
         "承担观察、翻译、计划、评审、清理迭代、排版及语音长度修订等专业判断。"
         "这些是按需调用的角色与阶段，不等于各自常驻一个进程。",
         "src/video_translation/execution/nodes.py",
         "src/video_translation/execution/adaptive.py",
         "src/video_translation/execution/adaptive_observe.py",
         "src/video_translation/execution/adaptive_audio.py"),
]

VIEWS = [
    view("system", "系统联动", [
        node("user", "用户", "human", "提出目标、核对需求、回答业务问题和验收候选。"),
        node("home", "首页 / 项目管家对话", "module", "项目消息与管理决定的唯一前台入口；具体制作进入任务中心。",
             "ui/src/pages/Management.tsx"),
        node("requirements", "需求中心", "module", "读取 Fireplay 需求并核对接单，确认后创建任务。",
             "src/video_translation/application/intake.py"),
        node("tasks", "任务中心", "module", "展示单任务流程、节点对话、问题、控制和验收。",
             "ui/src/pages/TaskDetail.tsx"),
        node("schedules", "定时触发器", "program", "内置巡检、需求刷新及自定义消息；同一计划防止重复排队。",
             "src/video_translation/scheduler/schedules.py"),
        node("events", "管理队列与本地扫描", "program", "持久化事件，优先处理用户消息，每30秒检查任务实质进展。",
             "src/video_translation/application/management.py"),
        deepcopy(ROLES[0]),
        node("guard", "授权与状态核验", "program", "核对动作确认、任务当前依据、费用额度及外部未知结果，再应用动作。",
             "src/video_translation/application/management.py",
             "src/video_translation/application/tasks.py"),
        node("worker", "Worker / NodeExecutor", "program", "推进就绪节点、保存状态、调用专业 Agent 与工具。",
             "src/video_translation/execution/worker.py", "src/video_translation/execution/nodes.py"),
        node("specialists", "节点协调 / Route / 制作判断", "agent", "三个专业角色族，详见 Agent 协作视图。",
             "src/video_translation/execution/coordination.py",
             "src/video_translation/execution/clean_routing.py",
             "src/video_translation/execution/adaptive.py"),
        node("resources", "资源与工作包", "program", "按任务快照提供提示词、知识、技能及已实现工具。",
             "src/video_translation/agent_runtime/resources.py"),
        node("tools", "模型服务 / 媒体工具", "tool", "ASR、TTS、图片、视频模型与本地媒体处理；外部请求记录费用与回执。",
             "src/video_translation/execution/nodes.py"),
        node("store", "SQLite / 文件证据", "store", "保存任务、管理事件、问题、动作、会话、外部请求和产物；重启恢复且不盲重放未知请求。",
             "src/video_translation/persistence/store.py"),
        node("delivery", "成品库", "module", "接收通过流程生成的交付记录；用户视听验收仍在任务中完成。",
             "ui/src/pages/WorkspacePages.tsx"),
    ], [
        ("user", "requirements", "核对接单"), ("user", "tasks", "协作 / 验收"),
        ("user", "home", "目标 / 管理决定"), ("home", "events", "管理消息"),
        ("schedules", "events", "定时事件"),
        ("requirements", "guard", "确认后创建"), ("events", "manager", "有限项目上下文"),
        ("manager", "guard", "动作建议"), ("tasks", "guard", "任务操作"),
        ("guard", "worker", "派工 / 控制"), ("worker", "specialists", "专业判断"),
        ("resources", "specialists", "资源包"), ("specialists", "tools", "工作单经程序执行"),
        ("worker", "store", "状态 / 证据"), ("events", "store", "事件 / 问答 / 审计"),
        ("store", "events", "检查任务变化"), ("worker", "delivery", "交付记录"),
    ]),
    view("ownership", "页面职责", [
        node("user", "用户", "human", "可通过首页讨论，也可进入对应业务页面直接操作。"),
        node("home", "首页", "module", "项目对话、简短运行摘要与待决定事项。没有第二份任务列表。",
             "ui/src/pages/Management.tsx", "ui/src/pages/WorkspacePages.tsx"),
        node("business", "任务 / 需求 / 成品", "module", "具体对象的唯一工作区，保存制作、接单与交付结果。",
             "ui/src/pages/TaskDetail.tsx", "ui/src/pages/FireplayRequirements.tsx"),
        node("automation", "自动化", "module", "保存轮询目标、时间、启停和触发结果；后台扫描独立运行。",
             "ui/src/pages/Schedules.tsx", "src/video_translation/scheduler/schedules.py"),
        node("services", "同一套业务服务与状态", "program", "管理建议和直接操作均经现有业务服务校验；不复制任务状态或预算。",
             "src/video_translation/application/management.py", "src/video_translation/application/tasks.py"),
        node("settings", "设置", "module", "模型、价格、能力资源与管家执行策略。",
             "ui/src/components/ModelSelection.tsx", "ui/src/components/ManagerSettings.tsx"),
        node("developer", "开发者视图", "module", "位于设置中，提供架构图和审计记录；不承担业务处理。",
             "ui/src/pages/Management.tsx"),
    ], [
        ("user", "home", "讨论 / 决定"), ("user", "business", "直接处理"),
        ("user", "automation", "保存轮询目标"), ("home", "services", "建议经确认"),
        ("business", "services", "具体业务操作"), ("automation", "services", "按计划触发"),
        ("settings", "services", "执行配置"), ("developer", "services", "只读架构与审计"),
    ]),
    view("agents", "Agent 协作", [
        node("user", "用户", "human", "管理目标和业务决定的来源。"),
        deepcopy(ROLES[0]), deepcopy(ROLES[1]),
        node("executor", "业务服务与 Worker", "program", "管理动作与协作操作须经业务规则校验；制作由节点执行器推进。",
             "src/video_translation/application/management.py", "src/video_translation/execution/nodes.py"),
        deepcopy(ROLES[2]), deepcopy(ROLES[3]),
        node("adapter", "AgentAdapter", "program", "按配置通过 Codex、Harness 或自定义命令执行独立结构化调用。",
             "src/video_translation/integrations/agents/adapter.py"),
        node("tools", "外部模型与本地工具", "tool", "专业判断形成计划，由执行器调用工具；结果回到专业判断与任务证据。"),
    ], [
        ("user", "manager", "项目消息 / 定时目标"), ("user", "coordinator", "节点消息"),
        ("manager", "executor", "确认后的管理动作"), ("coordinator", "executor", "校验后的协作操作"),
        ("executor", "route", "清理路线判断"), ("route", "work", "有边界的工作单"),
        ("executor", "work", "阶段判断 / 迭代"), ("work", "tools", "程序落实工作单"),
        ("manager", "adapter", "管理调用"), ("coordinator", "adapter", "协调调用"),
        ("route", "adapter", "路线调用"), ("work", "adapter", "制作调用"),
    ]),
    view("sessions", "会话与记录", [
        node("user", "用户消息 / 定时消息", "human", "消息提交后持久化排队，不把已排队显示为已完成。"),
        node("management", "项目管理逻辑会话", "session", "manager_event / manager_question / manager_action / manager_operation。"
             "同一轮询计划使用固定讨论范围，回答不会自动升级为所有任务的全局规则。",
             "src/video_translation/application/management.py"),
        node("coordination", "节点协作会话", "session", "每个任务节点独立保存对话、协调请求和问题，回答关联对应节点与尝试。",
             "src/video_translation/execution/coordination.py"),
        node("production", "专业制作会话", "session", "production_session / observation_session 保存轮次、候选、接受或延期结果；独立于聊天记录。",
             "src/video_translation/execution/adaptive.py",
             "src/video_translation/execution/adaptive_observe.py"),
        node("packet", "有边界的调用工作包", "program", "程序读取对应事实、历史回答与资源，构造本次调用上下文；不依赖桌面聊天自动共享记忆。",
             "src/video_translation/agent_runtime/resources.py",
             "src/video_translation/application/management.py"),
        node("call", "独立结构化模型调用", "agent", "每次调用有明确输入、输出 schema、状态和证据；结果未知时先保留现场。",
             "src/video_translation/integrations/agents/adapter.py"),
        node("records", "项目持久化记录", "store", "项目维护逻辑会话与状态。管理调用、制作调用和外部付费工具分别计数与留证。",
             "src/video_translation/persistence/store.py"),
    ], [
        ("user", "management", "全局 / 计划范围"), ("user", "coordination", "任务节点范围"),
        ("management", "packet", "管理事实"), ("coordination", "packet", "节点事实"),
        ("production", "packet", "轮次 / 候选"), ("packet", "call", "受限上下文"),
        ("call", "records", "结果 / 问题 / 回执"), ("records", "management", "后续轮次读取"),
        ("records", "coordination", "回答后继续"), ("records", "production", "迭代恢复"),
    ]),
]


def architecture():
    return deepcopy({
        "role_count": len(ROLES), "session_count": 3,
        "count_basis": "4 类 Agent 职责，按需调用；3 类逻辑会话。轮询计划是项目管理中的独立讨论范围。",
        "roles": ROLES, "modules": MODULES, "views": VIEWS,
        "implemented": [
            "全局状态扫描、异常去重、需求刷新、管理问答和调用上限。",
            "内置定时管理与自定义消息轮询，逐轮保存输入、回复、错误和真实状态。",
            "管理建议确认、当前任务依据复核、未知外部结果保护；明确启用后可自动暂停。",
            "节点协作、局部路线与专业制作迭代；候选、费用和操作留证。",
        ],
        "planned": [
            "稳定问题状态与工作单；转交节点制作或维护 Agent，复核结果后关闭问题。维护 Agent 尚未接入。",
            "带范围、额度、幂等与审批策略的自动接单；目前需在需求页核对并确认。",
            "多执行器分配、跨机器调度、管理策略评估与更多项目级事件订阅。",
            "细粒度授权策略与通知；自动发布、代码自修复和代替用户验收尚未开放。",
        ],
    })
