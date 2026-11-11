import dagre from "@dagrejs/dagre";
import type { TaskNode } from "../lib/types";

export const NODE_WIDTH = 232;
export const NODE_HEIGHT = 104;

/** Hide only transitive edges; every prerequisite remains reachable in the graph. */
export function visibleDependencies(nodes: TaskNode[]) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const reaches = (from: string, target: string, visited = new Set<string>()): boolean => {
    if (from === target) return true;
    if (visited.has(from)) return false;
    visited.add(from);
    return (byId.get(from)?.depends_on ?? []).some((id) => reaches(id, target, visited));
  };
  return nodes.flatMap((node) => node.depends_on
    .filter((dep) => byId.has(dep))
    .filter((dep) => !node.depends_on.some((other) => other !== dep && reaches(other, dep)))
    .map((dep) => ({ source: dep, target: node.id })));
}

/** Dagre ranks both nodes and long-edge waypoints from actual dependencies. */
export function layoutWorkflow(nodes: TaskNode[]) {
  const graph = new dagre.graphlib.Graph();
  graph.setGraph({ rankdir: "TB", nodesep: 64, ranksep: 72, edgesep: 24, marginx: 32, marginy: 32 });
  graph.setDefaultEdgeLabel(() => ({}));
  nodes.forEach(node => graph.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT }));
  const dependencies = visibleDependencies(nodes);
  dependencies.forEach(edge => graph.setEdge(edge.source, edge.target));
  dagre.layout(graph);
  return {
    positions: nodes.map(node => {
      const point = graph.node(node.id);
      return { id: node.id, x: point.x - NODE_WIDTH / 2, y: point.y - NODE_HEIGHT / 2 };
    }),
    routes: new Map(dependencies.map(edge => [ `${edge.source}--${edge.target}`, graph.edge(edge.source, edge.target).points ])),
    width: graph.graph().width ?? 300,
    height: graph.graph().height ?? 240,
  };
}
