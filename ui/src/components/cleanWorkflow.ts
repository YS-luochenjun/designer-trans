import type { ProductionSession, TaskNode } from "../lib/types";
import type { Edge } from "@xyflow/react";
import { MarkerType } from "@xyflow/react";
import { NODE_HEIGHT, NODE_WIDTH } from "./workflowLayout";

export const cleanMethodGroups = [
  { id: "local", label: "本地补底与跟踪", role: "本地工具", hint: "均匀填补 / 供体 / 表面 / 跟踪",
    detail: "flat、donor、local_surface、tracked_donor。按背景选择填补、干净供体或低纹理表面拟合；已实现基础跟踪与部分方法的前景保护，不等于自动分割或复杂纹理重建。" },
  { id: "video", label: "Wan 局部视频编辑", role: "云端工具 · 按需", hint: "同镜头短段 · 额度内调用",
    detail: "仅在任务策略、额度和服务允许时使用。裁取同镜头的局部短段，保存原请求与回执，校正返回视频的尺寸和帧时序后贴回；结果未知时先核对原请求。" },
  { id: "image", label: "图像编辑 / 参考图补底", role: "图像工具 · 按需", hint: "单帧辅助 · 另查连续性",
    detail: "image_edit 或 image_reference → reference_plate，依任务授权和实际工具可用性启用。参考图登记后仅用于适用的局部短段；单张干净图不能证明连续视频已修好。" },
];

const steps = [
  { id: "inventory", label: "读取待替换文字与设计", x: 370, y: 32, role: "原片共享", hint: "位置 / 时段 / 样式 / 运动",
    detail: "来自理解原片与文字设计节点，仅清理标记为需要翻译替换的原文字；不是默认删除全部标识和文字。" },
  { id: "inspect", label: "观察原片与当前底片", x: 370, y: 202, role: "Work Agent", hint: "真实帧 / 边界 / 运动证据",
    detail: "核对原片、当前底片和已有候选，明确真实文字范围、同镜头时段及需要保护的主体；可补查帧、查阅方法和修订范围。" },
  { id: "choose", label: "选择方法与局部范围", x: 370, y: 372, role: "Work Agent", hint: "以下方法择一 · 可换法",
    detail: "按已观察证据、可用工具和任务策略选择一种方法。三条工具分支是备选路线，不是每次必须依次调用的三个步骤。" },
  ...cleanMethodGroups.map((group, index) => ({ ...group, x: 62 + index * 308, y: 558 })),
  { id: "compose", label: "贴回全时长候选", x: 370, y: 744, role: "媒体执行器", hint: "保留当前底片 · 绑定版本",
    detail: "仅将本次局部修复贴回当前已接受底片，记录基底、候选与原回执。原始视频和已接受结果保留，未审候选不会自动成为底片。" },
  { id: "compare", label: "局部修复合格？", x: 370, y: 914, role: "Work Agent", hint: "残字 / 主体 / 边界 / 连续性",
    detail: "对照实际候选，检查残字、主体损伤、接缝和闪烁；质量不明先补证据，不合格保留记录并返回观察。抽样帧不等于已完整播放视频。" },
  { id: "accept", label: "接受局部 · 更新底片", x: 370, y: 1084, role: "局部接受", hint: "只接受本次已核验时空范围",
    detail: "接受的是当前候选的局部范围，不是整段文字或整片视频自动验收。后续修复继续基于更新后的底片。" },
  { id: "coverage", label: "所有文字时段均覆盖？", x: 370, y: 1254, role: "程序核验", hint: "未覆盖 / 暂缓仍算未完成",
    detail: "按各待替换文字的真实时段核算接受范围。未覆盖或暂缓的部分返回观察继续处理，覆盖率不能代替整体质量结论。" },
  { id: "review", label: "整体验证通过？", x: 370, y: 1424, role: "Work Agent + 程序核验", hint: "全部文字 ID / 当前底片 / 证据",
    detail: "清底完成判断必须绑定当前底片 SHA，覆盖全部待替换文字并通过质量校验。局部高分和生成成功不能替代本次整体验证。" },
  { id: "finish", label: "输出共用无字底片", x: 370, y: 1594, role: "原片共享产物", hint: "合格底片供各语言共用",
    detail: "通过清底核验后输出共用底片。仍须在后续成片审核中完整视听；带问题审看稿不会被作为合格底片放行到译文字层。" },
  { id: "typography", label: "各语言重建译文字层", x: 370, y: 1764, role: "后续节点 · 清底之外", hint: "另依赖语言适配与配音",
    detail: "这是清底之后的主流程节点。每种语言使用共用底片，结合语言适配、文字设计与配音时间重建译文字层，再进入画面对照和合成。" },
  { id: "route", label: "Route 分析能力障碍", x: 678, y: 202, role: "条件分支 · Route Agent", hint: "安排有依据的局部补查",
    detail: "剩余工作暂缓且存在新的能力或证据检查时，Route 分析障碍并派发有边界的工作单。它不修改像素，不豁免质量，不提高预算，也不是项目管家。" },
  { id: "blocked", label: "保留进度与未解决项", x: 678, y: 32, role: "受阻 / 待处理", hint: "缺能力、额度或需人工决定",
    detail: "没有可行方案时保存当前底片、候选证据与未解决范围，等待补充能力或明确处理；不能把暂缓、生成回执或带问题审看稿当成清底完成。" },
];

// This is an execution-stage projection, not a second queue of runnable nodes.
export function cleanWorkflow(parent: TaskNode, session?: ProductionSession) {
  const history = session?.history ?? [];
  const last = history.at(-1)?.decision;
  const action = String(last?.action ?? "");
  const method = String(last?.method ?? "");
  let current = "inventory";
  if (session) current = ["inspect", "inspect_metadata", "inspect_pipeline", "inspect_archive", "revise_scope", "consult", "track", "confirm_scope"].includes(action) ? "inspect"
    : action === "accept" ? "coverage" : action === "finish" ? "review"
    : session.candidate ? "compare" : "choose";
  if (parent.status === "waiting_external" && action === "repair") {
    current = method === "wan" ? "video" : ["image_edit", "reference_plate"].includes(method) ? "image" : "local";
  }
  if (parent.status === "waiting_external" && action === "request_reference") current = "image";
  if (session?.routing_stop && ["blocked", "waiting_user"].includes(parent.status)) current = "blocked";
  const complete = parent.status === "succeeded";
  if (complete) current = "finish";
  const positions = steps.map(({ id, x, y }) => ({ id, x, y }));
  const nodes: TaskNode[] = steps.map(({ id, label }) => ({
    ...parent, id, label, definition_id: id, depends_on: [], artifact_ids: [], error: null,
    status: parent.status === "skipped" ? "skipped"
      : parent.status === "cancelled" ? "cancelled"
      : complete && id === "finish" ? "succeeded"
      : id === current ? parent.status
      : "workflow_step",
  }));
  const edges: Edge[] = [];
  type Point = { x: number; y: number };
  const route = (source: string, target: string, label?: string, via?: Point[], color = "#7e9690") => {
    const s = positions.find(p => p.id === source)!;
    const t = positions.find(p => p.id === target)!;
    const points = via ?? [{ x: s.x + NODE_WIDTH / 2, y: s.y + NODE_HEIGHT },
      { x: t.x + NODE_WIDTH / 2, y: t.y }];
    const middle = Math.floor((points.length - 1) / 2);
    edges.push({ id: `${source}--${target}`, source, target, type: "dependency",
      markerEnd: { type: MarkerType.ArrowClosed, color },
      style: { stroke: color, strokeWidth: 1.5 },
      data: { points, exact: true, label, labelX: (points[middle].x + points[middle + 1].x) / 2 + 8,
        labelY: (points[middle].y + points[middle + 1].y) / 2 - 8 } });
  };
  route("inventory", "inspect");
  route("inspect", "choose");
  cleanMethodGroups.forEach(group => {
    const step = positions.find(p => p.id === group.id)!;
    route("choose", group.id, undefined, [{ x: 486, y: 476 }, { x: 486, y: 516 },
      { x: step.x + 116, y: 516 }, { x: step.x + 116, y: 558 }]);
    route(group.id, "compose", undefined, [{ x: step.x + 116, y: 662 },
      { x: step.x + 116, y: 704 }, { x: 486, y: 704 }, { x: 486, y: 744 }]);
  });
  route("compose", "compare");
  route("compare", "accept", "通过");
  route("accept", "coverage");
  route("coverage", "review", "是");
  route("review", "finish", "通过");
  route("finish", "typography");
  const loop = (source: string, label: string, lane: number) => {
    const step = positions.find(p => p.id === source)!;
    route(source, "inspect", label, [{ x: 370, y: step.y + 52 }, { x: lane, y: step.y + 52 },
      { x: lane, y: 254 }, { x: 370, y: 254 }], "#c54545");
    Object.assign(edges.at(-1)!.data!, { labelX: (370 + lane) / 2 - 48, labelY: step.y + 44 });
  };
  loop("compare", "不合格 / 补证据", 36);
  loop("coverage", "未覆盖", 16);
  loop("review", "未通过", -4);
  route("choose", "route", "全部暂缓", [{ x: 602, y: 424 }, { x: 950, y: 424 },
    { x: 950, y: 254 }, { x: 910, y: 254 }], "#a2763c");
  route("route", "inspect", "补查", [{ x: 678, y: 254 }, { x: 602, y: 254 }], "#a2763c");
  route("route", "blocked", "无可行方案", [{ x: 794, y: 202 }, { x: 794, y: 136 }], "#a2763c");
  return { nodes, edges, positions, routes: new Map(), width: 990, height: 1900, current,
    steps: new Map(steps.map(step => [step.id, step])),
    progress: session ? `局部接受 ${session.accepted.length} 项 · 判断 ${session.turn} 轮` : "尚未开始" };
}
