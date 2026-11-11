import { useMemo, useState } from "react";
import dagre from "@dagrejs/dagre";
import { Background, Handle, MarkerType, Position, ReactFlow } from "@xyflow/react";
import type { Node, NodeProps, ReactFlowInstance } from "@xyflow/react";
import { Focus, Minus, Plus, ScanLine } from "lucide-react";
import { useData } from "../lib/api";
import { ErrorBox, Loading } from "./common";
import { cleanMethodGroups } from "./cleanWorkflow";
import "@xyflow/react/dist/style.css";
import "./ProjectArchitecture.css";

type Item = { id: string; label: string; kind: string; detail: string; sources: string[] };
type View = { id: string; title: string; nodes: Item[]; edges: { source: string; target: string; label: string }[] };
type Catalog = {
  role_count: number; session_count: number; count_basis: string; roles: Item[];
  modules: { name: string; responsibility: string; href: string }[];
  views: View[]; implemented: string[]; planned: string[];
};
type ArchitectureNode = Node<Item & { [key: string]: unknown }>;
const kinds: Record<string, string> = {
  human: "用户", agent: "Agent", module: "模块", program: "程序", tool: "工具", store: "存储", session: "逻辑会话",
};

function GraphNode({ data }: NodeProps<ArchitectureNode>) {
  return <div className={`architecture-node architecture-node--${data.kind}`}>
    <Handle type="target" position={Position.Top} />
    <span>{kinds[data.kind]}</span><strong>{data.label}</strong>
    <Handle type="source" position={Position.Bottom} />
  </div>;
}
const nodeTypes = { architecture: GraphNode };

export function ProjectArchitecture() {
  const { data, loading, error } = useData<Catalog>("/manager/architecture");
  const [selected, setSelected] = useState("system");
  if (loading) return <Loading />;
  if (error || !data) return <ErrorBox message={error || "无法读取项目架构"} />;
  const view = data.views.find(v => v.id === selected) ?? data.views[0];
  return <div className="project-architecture">
    <div className="architecture-summary">
      <strong>{data.role_count} 类 Agent 职责</strong><strong>{data.session_count} 类逻辑会话</strong>
      <p>{data.count_basis}</p>
    </div>
    <div className="tabs" role="tablist" aria-label="架构视图">
      {data.views.map(v => <button key={v.id} role="tab" aria-selected={v.id === selected}
        className={v.id === selected ? "active" : ""} onClick={() => setSelected(v.id)}>{v.title}</button>)}
    </div>
    <ArchitectureGraph key={view.id} view={view} />
    <section className="panel architecture-section">
      <div className="architecture-clean-heading"><h2>清底执行流程</h2><a href="#tasks">前往任务中心 ›</a></div>
      <p>打开任务，在“局部清字与补底”节点点击“进入”，查看方法分支、局部验收与障碍处理。它是制作子流程，项目管家负责项目管理。</p>
      <details><summary>现有工具与能力边界</summary>
        <div className="architecture-table-wrap"><table><thead><tr><th>工具路线</th><th>适用与边界</th></tr></thead>
          <tbody>{cleanMethodGroups.map(group => <tr key={group.id}><th>{group.label}</th><td>{group.detail}</td></tr>)}</tbody>
        </table></div>
        <p>可用工具由任务授权、额度与运行环境决定；专用时序修复引擎尚未正式接入。</p>
      </details>
    </section>
    <section className="panel architecture-section">
      <h2>模块职责</h2>
      <div className="architecture-table-wrap"><table><thead><tr><th>模块</th><th>负责内容</th></tr></thead>
        <tbody>{data.modules.map(m => <tr key={m.name}><th><a href={m.href}>{m.name}</a></th><td>{m.responsibility}</td></tr>)}</tbody></table></div>
    </section>
    <section className="panel architecture-section">
      <h2>Agent 职责</h2>
      <div className="architecture-roles">{data.roles.map(r => <article key={r.id}>
        <h3>{r.label}</h3><p>{r.detail}</p>
      </article>)}</div>
    </section>
    <div className="architecture-capabilities">
      <section className="panel architecture-section"><h2>已实现</h2><ul>{data.implemented.map(s => <li key={s}>{s}</li>)}</ul></section>
      <section className="panel architecture-section"><h2>后续规划</h2><ul>{data.planned.map(s => <li key={s}>{s}</li>)}</ul></section>
    </div>
  </div>;
}

function ArchitectureGraph({ view }: { view: View }) {
  const [selected, setSelected] = useState(view.nodes[0].id);
  const [flow, setFlow] = useState<ReactFlowInstance<ArchitectureNode> | null>(null);
  const diagram = useMemo(() => {
    const graph = new dagre.graphlib.Graph();
    graph.setGraph({ rankdir: "TB", nodesep: 65, ranksep: 95, marginx: 36, marginy: 36 });
    graph.setDefaultEdgeLabel(() => ({}));
    view.nodes.forEach(n => graph.setNode(n.id, { width: 236, height: 88 }));
    view.edges.forEach(e => {
      if (e.source !== "records" && !(e.source === "store" && e.target === "events")) {
        graph.setEdge(e.source, e.target);
      }
    });
    dagre.layout(graph);
    return {
      nodes: view.nodes.map(n => ({ id: n.id, type: "architecture", data: { ...n },
        position: { x: graph.node(n.id).x - 118, y: graph.node(n.id).y - 44 } })),
      edges: view.edges.map((e, index) => ({ ...e, id: `${view.id}-${index}`, type: view.id === "ownership" ? "default" : "smoothstep",
        markerEnd: { type: MarkerType.ArrowClosed }, labelBgPadding: [7, 4] as [number, number],
        style: { stroke: "var(--color-muted)", strokeWidth: 1.4 },
        labelStyle: { fill: "var(--color-ink)", fontSize: 12 },
        labelBgStyle: { fill: "var(--color-surface)", fillOpacity: 0.95 } })),
    };
  }, [view]);
  const item = view.nodes.find(n => n.id === selected)!;
  const readable = () => {
    const node = diagram.nodes.find(n => n.id === selected)!;
    void flow?.setCenter(node.position.x + 118, node.position.y + 44, { zoom: 1 });
  };
  return <section className="panel architecture-diagram">
    <div className="architecture-toolbar">
      <h2>{view.title}</h2>
      <div className="actions wrap">
        <button className="secondary" onClick={() => void flow?.fitView({ padding: 0.12 })}><ScanLine size={16} />总览</button>
        <button className="secondary" onClick={readable}><Focus size={16} />清晰阅读</button>
        <button className="icon-button" aria-label="缩小架构图" onClick={() => void flow?.zoomOut()}><Minus size={16} /></button>
        <button className="icon-button" aria-label="放大架构图" onClick={() => void flow?.zoomIn()}><Plus size={16} /></button>
      </div>
    </div>
    <div className="architecture-canvas" role="region" aria-label={`${view.title}架构图`}>
      <ReactFlow<ArchitectureNode> nodes={diagram.nodes.map(n => ({ ...n, selected: n.id === selected }))}
        edges={diagram.edges} nodeTypes={nodeTypes}
        minZoom={0.15} maxZoom={1.8} nodesDraggable={false} nodesConnectable={false}
        onInit={instance => {
          setFlow(instance);
          if (view.id === "ownership") {
            void instance.fitView({ padding: 0.12 });
            return;
          }
          const first = diagram.nodes[0];
          void instance.setCenter(first.position.x + 118, first.position.y + 224, { zoom: 0.8 });
        }} onNodeClick={(_, node) => setSelected(node.id)}
        proOptions={{ hideAttribution: true }}>
        <Background gap={24} color="var(--color-line)" />
      </ReactFlow>
    </div>
    <div className="architecture-detail">
      <label>查看模块<select value={selected} onChange={e => setSelected(e.target.value)}>
        {view.nodes.map(n => <option key={n.id} value={n.id}>{n.label}</option>)}
      </select></label>
      <div><h3>{item.label}</h3><p>{item.detail}</p>
        {!!item.sources.length && <details><summary>代码位置</summary><ul>{item.sources.map(s => <li key={s}><code>{s}</code></li>)}</ul></details>}
      </div>
    </div>
  </section>;
}
