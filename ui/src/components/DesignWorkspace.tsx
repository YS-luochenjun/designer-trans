import type { Task } from "../lib/types";
import { labels } from "../lib/types";
import "./DesignerWorkspace.css";
import { FaceFusionWorkspace } from "./FaceFusionWorkspace";

export function DesignWorkspace({ task, onNode, refresh }: {
  task: Task; onNode: (id: string) => void; refresh: () => void;
}) {
  const kind = task.task_kind ?? "video_translation";
  if (kind === "video_translation") return null;
  if (kind === "face_swap") return <FaceFusionWorkspace key={task.external_execution?.id ?? task.id}
    task={task} refresh={refresh} />;
  const workspace = task.design_workspace;
  if (!workspace) return <section className="panel designer-workspace" role="status">
    新任务的素材与流程信息暂未返回。
  </section>;
  return <section className="panel designer-workspace" aria-label="图片翻译工作区">
    <h2>来源与语言矩阵</h2>
    <p className="muted">
      {workspace.source_count} 张来源图 × {workspace.locale_count} 种语言 = {workspace.member_count} 个交付成员
    </p>
    <div className="design-matrix-scroll" tabIndex={0} role="region" aria-label="图片交付矩阵">
      <table className="design-matrix"><thead><tr><th scope="col">来源图</th>
        {task.target_locales.map(locale => <th scope="col" key={locale}>{locale}</th>)}
      </tr></thead><tbody>{task.asset_ids.map(source => <tr key={source}>
        <th scope="row">{task.assets?.find(asset => asset.id === source)?.name ?? source}</th>
        {task.target_locales.map(locale => {
          const member = workspace.members?.find(item => item.source_id === source && item.locale === locale);
          return <td key={locale}>{member
            ? <button onClick={() => onNode(member.id)}>{labels[member.status] ?? member.status}</button> : "待登记"}</td>;
        })}
      </tr>)}</tbody></table>
    </div>
  </section>;
}
