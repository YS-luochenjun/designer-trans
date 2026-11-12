import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ReactFlow, Background, Handle, Position, BaseEdge, MarkerType } from "@xyflow/react";
import type { Edge, EdgeProps, Node, NodeProps, ReactFlowInstance } from "@xyflow/react";
import { Focus, Maximize2, Minus, Plus, ScanLine } from "lucide-react";
import type { AgentTrace, Asset, ProductionSession, TaskNode } from "../lib/types";
import { nodeProgress } from "./NodeOverview";
import { cleanWorkflow } from "./cleanWorkflow";
import { activityLabel } from "./AgentActivity";
import { Status } from "./common";
import { Select } from "./Select";
import { layoutWorkflow, visibleDependencies, NODE_HEIGHT, NODE_WIDTH } from "./workflowLayout";
import "@xyflow/react/dist/style.css";
import "./WorkflowGraph.css";

type Props = { nodes: TaskNode[]; assets?: Asset[]; traces?: AgentTrace[]; sessions?: ProductionSession[]; selected: string | null; onSelect: (id: string) => void };
type FlowData = { node: TaskNode; activity: string; progress: string; role?: string; onSelect: (id: string) => void; onEnter?: (id: string) => void };
type DiagramNode = Node<FlowData>;
const activeStates = new Set(["running", "waiting_user", "waiting_external", "failed", "blocked", "unknown", "interrupted"]);

function WorkflowNode({ data, selected }: NodeProps<DiagramNode>) {
  const node = data.node;
  const clickTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (clickTimer.current) clearTimeout(clickTimer.current); }, []);
  const label = node.label.replace(/^[①②③④⑤⑥⑦⑧⑨⑩]\s*/, "");
  return <div className={`workflow-node nodrag workflow-node--${node.status} ${selected ? "is-selected" : ""}`}
    role="button" tabIndex={0} aria-pressed={Boolean(selected)} data-node-id={node.id}
    aria-label={`查看${label} · ${data.role ?? node.locale ?? "原片共享"}`} onClick={() => {
      if (!data.onEnter || node.definition_id !== "clean") { data.onSelect(node.id); return; }
      if (clickTimer.current) clearTimeout(clickTimer.current);
      clickTimer.current = setTimeout(() => data.onSelect(node.id), 250);
    }}
    onDoubleClick={() => {
      if (clickTimer.current) clearTimeout(clickTimer.current);
      data.onEnter?.(node.id);
    }}
    onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); data.onSelect(node.id); } }}>
    <Handle type="target" position={Position.Top} />
    <div className="workflow-node-top"><span>{data.role ?? node.locale ?? "原片共享"}</span>
      {node.route_state && ["failed", "interrupted", "waiting_user"].includes(node.status)
        ? <span className="status">Route {node.route_state === "running" ? "协调中" :
          node.route_state === "blocked" ? "遇到阻塞" : "待处理"}</span> : <Status value={node.status} />}</div>
    <strong className="workflow-node-label" data-testid="workflow-node-label" title={label}>{label}</strong>
    <div className="workflow-node-foot" title={node.error || data.activity || node.skip_reason || ""}>
      <span title={data.activity}>{data.progress}</span>
      {data.onEnter && node.definition_id === "clean" && <button className="subflow-enter" aria-label="进入清底子流程"
        onClick={event => { event.stopPropagation(); data.onEnter?.(node.id); }}>进入 ›</button>}
    </div>
    <Handle type="source" position={Position.Bottom} />
  </div>;
}

function DependencyEdge(props: EdgeProps) {
  const route = (props.data?.points ?? []) as { x: number; y: number }[];
  const points = props.data?.exact ? route : [{ x: props.sourceX, y: props.sourceY }, ...route.slice(1, -1),
    { x: props.targetX, y: props.targetY }];
  const path = points.map((point, index) => `${index ? "L" : "M"} ${point.x} ${point.y}`).join(" ");
  return <g data-edge-source={props.source} data-edge-target={props.target}>
    <BaseEdge id={props.id} path={path} style={props.style} markerEnd={props.markerEnd} />
    {typeof props.data?.label === "string" && <text x={Number(props.data.labelX)} y={Number(props.data.labelY)}
      fontSize={11} fill={props.style?.stroke} stroke="var(--color-graph)" strokeWidth={4} paintOrder="stroke">{props.data.label}</text>}
  </g>;
}
const nodeTypes = { workflow: WorkflowNode };
const edgeTypes = { dependency: DependencyEdge };

export function WorkflowGraph({ nodes, assets = [], traces = [], sessions = [], selected, onSelect }: Props) {
  nodes = nodes.map(node => node.definition_id === "clean" && node.status === "succeeded" &&
    (node.output.completion_state === "best_available" || node.output.quality_passed === false)
    ? { ...node, status: "failed", error: "清底未完成；旧接续记录不代表质量通过" } : node);
  const [inside, setInside] = useState<string | null>(null);
  const parent = nodes.find(node => node.id === inside);
  useEffect(() => { if (selected && selected !== inside) setInside(null); }, [selected, inside]);
  const enter = (id: string) => {
    if (nodes.find(node => node.id === id)?.definition_id !== "clean") return;
    onSelect(id); setInside(id);
  };
  const assetIds = [...new Set(nodes.map((node) => node.asset_id))];
  const [assetChoice, setAssetChoice] = useState("");
  const [localeChoice, setLocaleChoice] = useState("");
  const assetId = assetIds.includes(assetChoice) ? assetChoice : assetIds[0];
  const locales = [...new Set(nodes.filter((node) => node.asset_id === assetId && node.locale).map((node) => node.locale!))];
  const locale = locales.includes(localeChoice) ? localeChoice : locales[0];
  const sourceName = (id: string) => assets.find((asset) => asset.id === id)?.name ?? `原片 ${assetIds.indexOf(id) + 1}`;
  const selectedNode = nodes.find((node) => node.id === selected);
  useEffect(() => {
    if (selectedNode) { setAssetChoice(selectedNode.asset_id); if (selectedNode.locale) setLocaleChoice(selectedNode.locale); }
  }, [selectedNode?.id, selectedNode?.asset_id, selectedNode?.locale]);
  const visible = nodes.filter((node) => node.asset_id === assetId && (!node.locale || node.locale === locale));
  const scope = `${assetId}:${locale}`;
  return <section className="workflow-graph" data-testid="workflow-graph" aria-label="任务流程图">
    <div className="workflow-scope">
      {parent && <button className="subflow-back" onClick={() => setInside(null)}>‹ 主流程</button>}
      {parent ? <><strong className="workflow-source">{parent.label.replace(/^[①②③④⑤⑥⑦⑧⑨⑩]\s*/, "")}</strong><span className="workflow-progress">Work 自主选法 · 程序核验与调度 · 状态示意</span></> : <>
      {assetIds.length > 1 ? <Select aria-label="流程图原片" value={assetId} onChange={(event) => { setAssetChoice(event.target.value); onSelect(""); }}>
        {assetIds.map((id) => <option key={id} value={id}>{sourceName(id)}</option>)}
      </Select> : <span className="workflow-source" title={assetId ? sourceName(assetId) : ""}>{assetId ? sourceName(assetId) : "执行流程"}</span>}
      {locales.length > 1 ? <Select aria-label="流程图语言" value={locale} onChange={(event) => { setLocaleChoice(event.target.value); onSelect(""); }}>
        {locales.map((value) => <option key={value} value={value}>{value}</option>)}
      </Select> : locale && <span className="workflow-locale">{locale}</span>}
      <span className="workflow-progress">{visible.filter((node) => ["succeeded", "completed", "skipped"].includes(node.status)).length} / {visible.length} 节点</span>
      </>}
    </div>
    {parent ? <Diagram key={`inside:${parent.id}`} nodes={visible} traces={traces} sessions={sessions}
      selected={selected} onSelect={() => onSelect(parent.id)} internal={cleanWorkflow(parent, sessions.find(s => s.node_id === parent.id))} /> :
      visible.length ? <Diagram key={scope} nodes={visible} traces={traces} sessions={sessions} selected={selected} onSelect={onSelect} onEnter={enter} /> :
      <div className="workflow-no-nodes">暂无流程节点</div>}
  </section>;
}

function Diagram({ nodes: outerNodes, traces = [], sessions = [], selected, onSelect, onEnter, internal }: Props & {
  onEnter?: (id: string) => void; internal?: ReturnType<typeof cleanWorkflow>;
}) {
  const nodes = internal?.nodes ?? outerNodes;
  const container = useRef<HTMLDivElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const [flow, setFlow] = useState<ReactFlowInstance<DiagramNode, Edge> | null>(null);
  const [zoom, setZoom] = useState(1);
  const [internalSelection, setInternalSelection] = useState<string | null>(null);
  const [wheelActive, setWheelActive] = useState(false);
  useEffect(() => {
    const activate = (event: PointerEvent) => setWheelActive(Boolean(container.current?.contains(event.target as globalThis.Node)));
    const release = (event: KeyboardEvent) => { if (event.key === "Escape") setWheelActive(false); };
    document.addEventListener("pointerdown", activate);
    document.addEventListener("keydown", release);
    return () => { document.removeEventListener("pointerdown", activate); document.removeEventListener("keydown", release); };
  }, []);
  const layout = useMemo(() => internal ?? layoutWorkflow(nodes), [nodes, internal]);
  const graph = useMemo(() => {
    const positions = new Map(layout.positions.map((position) => [position.id, position]));
    return {
      nodes: nodes.map((node): DiagramNode => ({ id: node.id, type: "workflow", selected: node.id === (internal ? internalSelection ?? internal.current : selected),
        position: { x: positions.get(node.id)!.x, y: positions.get(node.id)!.y },
        handles: [
          { type: "target", position: Position.Top, x: NODE_WIDTH / 2 - 3, y: -3, width: 6, height: 6 },
          { type: "source", position: Position.Bottom, x: NODE_WIDTH / 2 - 3, y: NODE_HEIGHT - 3, width: 6, height: 6 },
        ],
        width: NODE_WIDTH, height: NODE_HEIGHT, data: { node, onSelect: internal ? id => { setInternalSelection(id); onSelect(id); } : onSelect, onEnter,
          role: internal?.steps.get(node.id)?.role,
          progress: internal ? internal.steps.get(node.id)?.hint ?? "" : nodeProgress(node, sessions, nodes), activity: activityLabel(
          traces.filter((trace) => trace.node_id === node.id && trace.attempt === node.attempt).at(-1)) } })),
      edges: internal?.edges ?? visibleDependencies(nodes).map(({ source, target }): Edge => ({
        id: `${source}--${target}`, source, target, type: "dependency",
        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: "var(--color-faint)" },
        className: selected === source || selected === target ? "is-highlighted" : "",
        data: { points: layout.routes.get(`${source}--${target}`) },
      })),
    };
  }, [nodes, traces, sessions, selected, onSelect, onEnter, layout, internal, internalSelection]);
  const readableView = useCallback(() => {
    if (!flow || !stage.current) return;
    const width = stage.current.clientWidth;
    const scale = Math.max(0.82, Math.min(1, (width - 32) / layout.width));
    const center = internal ? 486 : layout.width / 2;
    void flow.setViewport({ x: width / 2 - center * scale, y: 28, zoom: scale });
  }, [flow, layout.width, Boolean(internal)]);
  useEffect(() => {
    readableView();
    if (!stage.current) return;
    const observer = new ResizeObserver(readableView);
    observer.observe(stage.current);
    return () => observer.disconnect();
  }, [readableView]);
  const locate = () => {
    const current = nodes.find((node) => node.id === (internal ? internalSelection ?? internal.current : selected)) ?? nodes.find((node) => activeStates.has(node.status)) ?? nodes[0];
    const position = layout.positions.find((item) => item.id === current?.id);
    if (position) void flow?.setCenter(position.x + NODE_WIDTH / 2, position.y + NODE_HEIGHT / 2, { zoom: 1 });
  };
  const overview = () => {
    if (!flow || !stage.current) return;
    const { clientWidth: width, clientHeight: height } = stage.current;
    const scale = Math.max(0.1, Math.min(1, (width - 32) / layout.width, (height - 32) / layout.height));
    void flow.setViewport({ x: (width - layout.width * scale) / 2,
      y: (height - layout.height * scale) / 2, zoom: scale });
  };
  const internalStep = internal?.steps.get(internalSelection ?? internal.current);
  return <>
    {internal && <div className="clean-flow-summary" aria-label="清底流程说明">
      <span>现有流程 · 方法择一</span><span>{internal.progress}</span>
    </div>}
    <div className={`workflow-diagram${wheelActive ? " is-wheel-active" : ""}`} ref={container}
      data-wheel-active={wheelActive} tabIndex={0} onFocus={() => setWheelActive(true)}
      data-testid="workflow-graph-viewport" role="region" aria-label="完整任务流程图">
      <div className="workflow-stage" ref={stage}>
      <ReactFlow<DiagramNode, Edge> nodes={graph.nodes} edges={graph.edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes}
        onInit={setFlow} onMove={(_, viewport) => setZoom(viewport.zoom)} minZoom={0.1} maxZoom={1.4}
        nodesDraggable={false} nodesConnectable={false} nodesFocusable={false} edgesFocusable={false}
        elementsSelectable={false} panOnScroll={wheelActive} zoomOnScroll={false} zoomOnDoubleClick={false}
        translateExtent={[[-64, -64], [layout.width + 64, layout.height + 104]]}
        preventScrolling={wheelActive} proOptions={{ hideAttribution: true }}>
        <Background gap={24} size={1} color="var(--color-line)" />
      </ReactFlow>
      </div>
    </div>
    <div className="workflow-toolbar" aria-label="流程图操作">
      <div className="workflow-zoom">
        <button type="button" aria-label="缩小流程图" title="缩小" disabled={!flow || zoom <= 0.101} onClick={() => void flow?.zoomOut()}><Minus size={16} /></button>
        <output aria-label="流程图缩放比例">{Math.round(zoom * 100)}%</output>
        <button type="button" aria-label="放大流程图" title="放大" disabled={!flow || zoom >= 1.399} onClick={() => void flow?.zoomIn()}><Plus size={16} /></button>
      </div>
      <button type="button" onClick={readableView} disabled={!flow}><ScanLine size={15} />阅读视图</button>
      <button type="button" onClick={overview} disabled={!flow}><Maximize2 size={15} />总览</button>
      <button type="button" onClick={locate} disabled={!flow}><Focus size={15} />定位节点</button>
    </div>
    {internalStep && <div className="clean-flow-detail" aria-live="polite">
      <strong>{internalStep.label}</strong><p>{internalStep.detail}</p>
    </div>}
  </>;
}
