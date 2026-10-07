import { useEffect, useState } from "react";
import { canDecideMcpApproval } from "../../utils/mcpApprovals";

const operationLabels = {
  ops_prepare_deployment: "部署预检查",
  ops_apply_deployment: "执行部署",
  ops_restart_programs: "重启程序",
  ops_upgrade_agents: "升级运维 Agent"
};
const statusLabels = { approved: "已批准本次调用", declined: "已拒绝", expired: "已失效" };

export default function McpApprovalCard({ approval, onDecide }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (approval.status !== "pending") return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [approval.status]);
  const canDecide = canDecideMcpApproval(approval, now);
  const status = approval.status === "pending" && !canDecide ? "expired" : approval.status;

  async function decide(decision) {
    if (busy || !canDecide) return;
    setBusy(true);
    setError("");
    try {
      await onDecide(approval, decision);
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="mcp-approval-card" aria-label="工具操作确认" aria-live="polite">
      <div className="mcp-approval-heading">
        <strong>{operationLabels[approval.tool_name] || "工具操作确认"}</strong>
        <span>{status === "pending" ? "等待你的确认" : statusLabels[status]}</span>
      </div>
      <p className="mcp-approval-server">{approval.server_name}{approval.tool_name ? ` · ${approval.tool_name}` : ""}</p>
      <p>{approval.description || approval.message}</p>
      {!approval.tool_name && approval.description ? <p>{approval.message}</p> : null}
      <details open>
        <summary>本次调用参数</summary>
        <pre>{JSON.stringify(approval.arguments, null, 2)}</pre>
      </details>
      {error ? <p className="mcp-approval-error" role="alert">{error}</p> : null}
      {canDecide ? (
        <>
          <p className="mcp-approval-note">仅批准本次调用；后续操作仍需单独确认。10 分钟内未确认将自动失效。</p>
          <div className="mcp-approval-actions">
            <button type="button" className="button button-secondary" disabled={busy} onClick={() => void decide("declined")}>拒绝</button>
            <button type="button" className="button button-primary" disabled={busy} onClick={() => void decide("approved")}>{busy ? "提交中…" : "批准本次调用"}</button>
          </div>
        </>
      ) : (
        <p className="mcp-approval-note">{status === "approved" ? "批准不代表执行完成，请以之后的工具结果为准。" : "此审批不会放行操作。"}</p>
      )}
    </section>
  );
}
