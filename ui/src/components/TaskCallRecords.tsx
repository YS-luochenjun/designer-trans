import type { Task } from "../lib/types";
import { time } from "../lib/types";
import { Empty, ErrorBox, JsonView, Status } from "./common";
import { Select } from "./Select";
import { ChevronDown } from "lucide-react";

const kindNames: Record<string, string> = {
  video_edit: "视频编辑", image_edit: "图片编辑", synthesize: "配音",
  audio_understanding: "音频理解", discussion: "节点讨论", imagegen: "内置图片工具执行",
};

export const imageRequestState = (value: string) => ({
  queued: "待项目执行器领取（尚未调用图片工具）",
  dispatch_blocked: "领取受阻（尚未调用图片工具）",
  claimed: "已领取，工具执行结果尚未返回",
  needs_reconciliation: "结果待核对，保留原请求不重复生成",
  succeeded: "参考图片已返回，仍需审阅贴回视频",
  failed: "请求失败",
}[value] ?? value);

export function TaskCallRecords({ task, nodeId, onNodeChange }: {
  task: Task; nodeId: string; onNodeChange: (id: string) => void;
}) {
  const calls = [...new Map((task.operations ?? []).map(call => [String(call.id), call])).values()]
    .filter(call => !nodeId || call.node_id === nodeId)
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  const requests = (task.imagegen_requests ?? []).filter(r => !nodeId || r.node_id === nodeId);
  return <details className="task-drawer task-call-records" id="task-calls">
    <summary>外部调用记录</summary>
    <div className="task-drawer-body">
      <div className="task-record-toolbar">
        <label>节点<Select aria-label="调用记录节点" value={nodeId} onChange={event => onNodeChange(event.target.value)}>
          <option value="">全部节点</option>
          {(task.nodes ?? []).map(node => <option key={node.id} value={node.id}>
            {node.label.replace(/^[①②③④⑤⑥⑦⑧⑨⑩]\s*/, "")} · {node.locale ?? "原片共享"}
          </option>)}
        </Select></label>
        <span>{calls.length} 条调用 · {requests.length} 条图片工具请求</span>
      </div>
      <div className="task-record-scroll" tabIndex={0} role="region" aria-label="外部调用明细">
        {requests.map(request => <details className="task-call-record" key={request.id}
          open={request.status === "pending" || request.status === "claimed"}>
          <summary><ChevronDown size={14} className="task-call-chevron" />
            <strong>内置图片工具 · {request.source_id}</strong>
            <span>{imageRequestState(request.lifecycle)}</span>
            <span>{time(request.created_at)}</span>
          </summary>
          <div className="task-call-detail">
            <p>执行通道：项目 Codex Worker · 实际领取者：{request.execution_owner ??
              (request.status === "pending" ? "尚未领取" : "历史回执未记录执行端")}</p>
            <p>处理位置：{(request.time_ms / 1000).toFixed(3)} 秒 · 第 {request.frame_index} 帧</p>
            <ErrorBox message={request.dispatch_blocked_reason ?? request.error ??
              request.execution_error_detail ?? request.execution_error ?? ""} />
            <details><summary>请求内容与产物位置</summary>
              <p style={{ whiteSpace: "pre-wrap" }}>{request.prompt}</p>
              <p>输入帧：{request.original_frame_path}</p>
              <p>返回图片：{request.generated_image_path ?? "尚无已登记结果"}</p>
              {request.generated_output && <>
                <p>工具原产物：{request.generated_output.original_path}</p>
                <p>参考图尺寸：{request.generated_output.dimensions.join(" × ")}；
                  原片帧尺寸：{request.generated_output.original_frame_dimensions.join(" × ")}。
                  文件接收保留原像素，仍需 Work 审阅视频。</p>
              </>}
              {request.tool_warning && <p>工具回执说明：{request.tool_warning}</p>}
              <p>领取回执：{request.claim_receipt_path ?? "尚未领取"}</p>
              <p>结果回执路径：{request.result_receipt_path ?? "尚未分配"}</p>
              <JsonView value={request} />
            </details>
          </div>
        </details>)}
        {!calls.length && !requests.length && <Empty title="尚无外部调用或工具请求" />}
        {calls.map(call => {
          const node = task.nodes?.find(item => item.id === call.node_id);
          return <details className="task-call-record" key={String(call.id)}>
            <summary>
              <ChevronDown size={14} className="task-call-chevron" />
              <strong>{call.provider === "agent" ? "Agent" : String(call.provider ?? "外部服务").toUpperCase()} · {kindNames[String(call.kind)] ?? String(call.kind ?? "调用")}</strong>
              <Status value={call.status === "unknown" && call.journal_state === "waiting" && call.resume_safe ? "waiting_external" : String(call.status)} />
              <span>{node?.label.replace(/^[①②③④⑤⑥⑦⑧⑨⑩]\s*/, "") ?? "节点未关联"} · {time(String(call.created_at ?? ""))}</span>
            </summary>
            <div className="task-call-detail">
              <ErrorBox message={typeof call.error === "string" ? call.error : ""} />
              <JsonView value={call} />
            </div>
          </details>;
        })}
      </div>
    </div>
  </details>;
}
