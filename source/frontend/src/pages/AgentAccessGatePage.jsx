import { useEffect, useState } from "react";
import { getAgentAccessStatus, getCurrentUser, requestAgentAccess } from "../services/authApi";

const containerStyle = {
  minHeight: "100vh",
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  justifyContent: "center",
  gap: "16px",
  padding: "24px",
  fontFamily: "var(--font-sans)"
};

const cardStyle = {
  display: "flex",
  flexDirection: "column",
  gap: "16px",
  width: "100%",
  maxWidth: "420px"
};

export default function AgentAccessGatePage({ user, onUserUpdated, onLogout }) {
  const [latestRequest, setLatestRequest] = useState(null);
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");

  useEffect(() => {
    let disposed = false;
    getAgentAccessStatus()
      .then((payload) => {
        if (!disposed) {
          setLatestRequest(payload?.latest_request || null);
        }
      })
      .catch(() => {});
    return () => {
      disposed = true;
    };
  }, []);

  async function handleSubmit(event) {
    event.preventDefault();
    if (submitting) {
      return;
    }
    setSubmitting(true);
    setErrorMessage("");
    try {
      const payload = await requestAgentAccess(reason);
      setLatestRequest(payload?.latest_request || null);
      setReason("");
      onUserUpdated({ ...user, agent_access: payload?.agent_access || "pending" });
    } catch (error) {
      setErrorMessage(error.message || "申请提交失败，请稍后重试。");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleRefresh() {
    if (refreshing) {
      return;
    }
    setRefreshing(true);
    setErrorMessage("");
    try {
      const freshUser = await getCurrentUser();
      if (freshUser && freshUser.agent_access !== user.agent_access) {
        onUserUpdated(freshUser);
        return;
      }
      const payload = await getAgentAccessStatus();
      setLatestRequest(payload?.latest_request || null);
      if (payload?.agent_access && payload.agent_access !== user.agent_access) {
        onUserUpdated({ ...user, agent_access: payload.agent_access });
      }
    } catch (error) {
      setErrorMessage(error.message || "状态刷新失败，请稍后重试。");
    } finally {
      setRefreshing(false);
    }
  }

  const agentAccess = user.agent_access || "none";

  if (agentAccess === "pending") {
    return (
      <div style={containerStyle}>
        <h2 style={{ margin: 0 }}>Agent 权限申请审核中</h2>
        <p style={{ margin: 0, color: "var(--text-soft)", textAlign: "center", maxWidth: "400px" }}>
          你的 Agent 使用权限申请已提交，管理员通过后即可使用 Agent。你仍然可以访问应用中心等通用页面。
        </p>
        {latestRequest?.reason ? (
          <p style={{ margin: 0, color: "var(--text-soft)", fontSize: "13px", textAlign: "center", maxWidth: "400px" }}>
            申请原因：{latestRequest.reason}
          </p>
        ) : null}
        {errorMessage ? <p style={{ margin: 0, color: "var(--color-danger, #d64545)" }}>{errorMessage}</p> : null}
        <div style={{ display: "flex", gap: "12px", marginTop: "8px" }}>
          <button className="button button-primary" onClick={handleRefresh} disabled={refreshing}>
            {refreshing ? "刷新中…" : "刷新状态"}
          </button>
          <button className="button button-secondary" onClick={onLogout}>
            退出登录
          </button>
        </div>
      </div>
    );
  }

  const rejected = agentAccess === "rejected";
  const rejectComment = rejected ? latestRequest?.review_comment : null;

  return (
    <div style={containerStyle}>
      <h2 style={{ margin: 0 }}>{rejected ? "Agent 权限申请未通过" : "申请使用 Agent"}</h2>
      <p style={{ margin: 0, color: "var(--text-soft)", textAlign: "center", maxWidth: "400px" }}>
        {rejected
          ? "你可以补充说明后重新提交申请。"
          : "你的账号已注册成功，可以使用应用中心等通用功能。Agent 需要管理员审批后才能使用，请填写申请原因。"}
      </p>
      {rejectComment ? (
        <p style={{ margin: 0, color: "var(--text-soft)", fontSize: "13px", textAlign: "center", maxWidth: "400px" }}>
          拒绝原因：{rejectComment}
        </p>
      ) : null}
      <form style={cardStyle} onSubmit={handleSubmit}>
        <textarea
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder={rejected ? "补充说明你需要使用 Agent 的原因…" : "例如：负责 XX 项目的需求文档编写，需要用 Agent 辅助产出 PRD…"}
          rows={4}
          maxLength={500}
          style={{ width: "100%", resize: "vertical" }}
        />
        {errorMessage ? <p style={{ margin: 0, color: "var(--color-danger, #d64545)" }}>{errorMessage}</p> : null}
        <div style={{ display: "flex", gap: "12px", justifyContent: "center" }}>
          <button className="button button-primary" type="submit" disabled={submitting || !reason.trim()}>
            {submitting ? "提交中…" : rejected ? "重新申请" : "提交申请"}
          </button>
          <button className="button button-secondary" type="button" onClick={onLogout}>
            退出登录
          </button>
        </div>
      </form>
    </div>
  );
}
