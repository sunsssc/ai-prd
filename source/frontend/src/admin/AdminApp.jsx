import { Fragment, useEffect, useState } from "react";
import MarkdownRenderer from "../components/common/MarkdownRenderer";
import MessageCard from "../components/common/MessageCard";
import { toMessageCard } from "../utils/assistantMessages";
import {
  formatBusinessDocConfidence,
  getBusinessDocItemDisplay,
  getBusinessDocReviewLabels,
  getHighConfidenceManualUpdates,
} from "../utils/businessDocRunDisplay";

const API_BASE = "/api/admin";

async function apiFetch(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw Object.assign(new Error(body.detail || "请求失败"), { status: res.status });
  }
  if (res.status === 204) return null;
  return res.json();
}

async function getAdminMe() {
  try {
    const data = await apiFetch("/auth/me");
    return data?.user || null;
  } catch (e) {
    if (e.status === 401) return null;
    throw e;
  }
}

async function listUsers(status) {
  const qs = status ? `?status=${status}` : "";
  return apiFetch(`/users${qs}`);
}

async function listImageUsage() {
  return apiFetch("/image-generation/usage");
}

async function businessDocFetch(path, options = {}) {
  const res = await fetch(`/api/business-docs${path}`, {
    credentials: "include",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || "请求失败");
  }
  return res.json();
}

function listBusinessDocRuns() {
  return businessDocFetch("/update-runs");
}

function getBusinessDocRun(runId) {
  return businessDocFetch(`/update-runs/${encodeURIComponent(runId)}`);
}

function startBusinessDocFullRun() {
  return businessDocFetch("/update-runs/full", { method: "POST" });
}

function retryBusinessDocRun(runId) {
  return businessDocFetch(`/update-runs/${encodeURIComponent(runId)}/retry`, { method: "POST" });
}

function getAdminBusinessDocUpdate(updateId) {
  return businessDocFetch(`/admin/updates/${encodeURIComponent(updateId)}`);
}

function applyAdminBusinessDocUpdate(updateId) {
  return businessDocFetch(`/admin/updates/${encodeURIComponent(updateId)}/apply`, { method: "POST" });
}

function ignoreAdminBusinessDocUpdate(updateId) {
  return businessDocFetch(`/admin/updates/${encodeURIComponent(updateId)}/ignore`, { method: "POST" });
}

const RUN_STATUS_LABELS = {
  pending: "排队中",
  running: "运行中",
  completed: "已完成",
  completed_with_errors: "部分失败",
  failed: "失败",
};

const BUSINESS_DOC_ITEM_TONES = {
  success: { color: "#3f684d", background: "#edf3ea", border: "#cfdcc9" },
  running: { color: "#93492f", background: "#f8eee8", border: "#e7c9ba" },
  attention: { color: "#775b24", background: "#f7f0dc", border: "#e2d2a4" },
  error: { color: "#8f2f2f", background: "#f8e8e5", border: "#e4bbb5" },
  pending: { color: "#5e5d59", background: "#f0eee6", border: "#dedbd0" },
};

function BusinessDocRunsPanel({
  runs,
  detail,
  updateDetail,
  loading,
  busy,
  updateLoading,
  updateBusy,
  updateAction,
  batchProgress,
  error,
  updateError,
  onStart,
  onSelect,
  onRetry,
  onRefresh,
  onOpenUpdate,
  onCloseUpdate,
  onApplyUpdate,
  onApplyHighConfidence,
  onIgnoreUpdate,
}) {
  const sidebarVisible = Boolean(detail || updateDetail || updateLoading);
  const highConfidenceUpdates = getHighConfidenceManualUpdates(detail?.items);
  const batchBusy = Boolean(batchProgress);
  return (
    <section>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "16px", marginBottom: "16px" }}>
        <div>
          <h2 style={{ margin: "0 0 6px", fontSize: "18px" }}>业务文档自动更新</h2>
          <p style={{ margin: 0, color: "var(--text-soft)", fontSize: "13px" }}>按最新代码全量校准，并查看增量更新、双 Agent 审核和自动应用记录。</p>
        </div>
        <div style={{ display: "flex", gap: "8px" }}>
          <button className="button button-secondary" disabled={loading || batchBusy} onClick={onRefresh}>刷新</button>
          <button className="button button-primary" disabled={busy || batchBusy} onClick={onStart}>按最新代码全量检查</button>
        </div>
      </div>
      {error ? <p style={{ color: "#721c24", background: "#f8d7da", padding: "8px 12px", borderRadius: "6px" }}>{error}</p> : null}
      {loading && !runs.length ? <p style={{ color: "var(--text-faint)" }}>加载中…</p> : null}
      <div style={{ display: "grid", gridTemplateColumns: sidebarVisible ? "minmax(0, 1fr) minmax(400px, 0.95fr)" : "1fr", alignItems: "start", gap: "16px" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "13px" }}>
          <thead>
            <tr style={{ borderBottom: "2px solid var(--line)", textAlign: "left" }}>
              <th style={{ padding: "10px" }}>来源</th><th style={{ padding: "10px" }}>状态</th><th style={{ padding: "10px" }}>进度</th><th style={{ padding: "10px" }}>时间</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.run_id} style={{ borderBottom: "1px solid var(--line-soft)", cursor: batchBusy ? "default" : "pointer" }} onClick={batchBusy ? undefined : () => onSelect(run.run_id)}>
                <td style={{ padding: "10px" }}>{run.mode === "full" ? "全量" : `增量 · ${run.repo_key || "-"}`}</td>
                <td style={{ padding: "10px" }}>{RUN_STATUS_LABELS[run.status] || run.status}</td>
                <td style={{ padding: "10px", fontVariantNumeric: "tabular-nums" }}>{run.completed_items + run.failed_items}/{run.total_items}</td>
                <td style={{ padding: "10px", color: "var(--text-soft)" }}>{formatDateTime(run.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {sidebarVisible ? (
          <aside style={{ border: "1px solid var(--line)", borderRadius: "12px", padding: "14px", background: "var(--surface-strong)", position: "sticky", top: "72px", maxHeight: "calc(100vh - 96px)", overflow: "hidden", display: "flex", flexDirection: "column" }}>
            {updateDetail || updateLoading ? (
              <>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "12px", paddingBottom: "12px", borderBottom: "1px solid var(--line-soft)" }}>
                  <div style={{ minWidth: 0 }}>
                    <button className="button button-secondary" style={{ fontSize: "12px", padding: "4px 9px", marginBottom: "9px" }} onClick={onCloseUpdate}>← 返回运行详情</button>
                    <h3 style={{ margin: 0, fontSize: "16px", fontFamily: "var(--font-serif)", fontWeight: 500 }}>业务文档更新</h3>
                    {updateDetail?.source_path ? <p style={{ margin: "5px 0 0", color: "var(--text-soft)", fontSize: "12px", overflowWrap: "anywhere" }}>{updateDetail.source_path}</p> : null}
                  </div>
                  {updateDetail?.status === "pending" ? (
                    <div style={{ display: "flex", gap: "6px", flexShrink: 0 }}>
                      <button className="button button-secondary" style={{ fontSize: "12px", padding: "5px 10px" }} disabled={updateBusy} onClick={onIgnoreUpdate}>忽略</button>
                      <button className="button button-primary" style={{ fontSize: "12px", padding: "5px 10px" }} disabled={updateBusy} onClick={onApplyUpdate}>
                        {updateAction === "apply" ? <><span className="button-spinner" aria-hidden="true" />应用中…</> : "应用"}
                      </button>
                    </div>
                  ) : null}
                </div>
                {updateError ? <p style={{ color: "#8f2f2f", background: "#f8e8e5", padding: "8px 10px", borderRadius: "8px", fontSize: "12px" }}>{updateError}</p> : null}
                {updateLoading ? (
                  <p style={{ color: "var(--text-faint)" }}>正在加载提案…</p>
                ) : updateDetail ? (
                  <>
                    <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", margin: "10px 0" }}>
                      <span style={{ color: "#775b24", background: "#f7f0dc", border: "1px solid #e2d2a4", borderRadius: "999px", padding: "3px 8px", fontSize: "12px" }}>
                        {updateDetail.status === "pending" ? "等待人工确认" : updateDetail.status === "applied" ? "已应用" : "已忽略"}
                      </span>
                      <span style={{ color: "var(--text-faint)", fontSize: "12px", padding: "3px 0" }}>{updateDetail.mode === "full" ? "全量校准" : "增量更新"}</span>
                      {updateDetail.confidence == null ? null : <span style={{ color: "var(--text-faint)", fontSize: "12px", padding: "3px 0" }}>判断置信度 {Math.round(updateDetail.confidence * 100)}%</span>}
                    </div>
                    <div className="review-content" style={{ flex: "1 1 auto", minHeight: 0, overflow: "auto", paddingRight: "6px" }}>
                      <MarkdownRenderer markdown={updateDetail.content || ""} variant="document" />
                    </div>
                  </>
                ) : null}
              </>
            ) : detail ? (
              <>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "8px" }}>
                  <h3 style={{ margin: 0, fontSize: "15px" }}>运行详情</h3>
                  <div style={{ display: "flex", justifyContent: "flex-end", flexWrap: "wrap", gap: "6px" }}>
                    {highConfidenceUpdates.length || batchBusy ? (
                      <button className="button button-primary" style={{ fontSize: "12px", padding: "5px 10px" }} disabled={busy || batchBusy} onClick={() => onApplyHighConfidence(highConfidenceUpdates)}>
                        {batchBusy ? <><span className="button-spinner" aria-hidden="true" />应用中 {batchProgress.completed}/{batchProgress.total}</> : `应用置信度 > 90%（${highConfidenceUpdates.length}）`}
                      </button>
                    ) : null}
                    {detail.failed_items > 0 ? <button className="button button-secondary" disabled={busy || batchBusy} onClick={() => onRetry(detail.run_id)}>重试失败任务</button> : null}
                  </div>
                </div>
                <p style={{ color: "var(--text-soft)", fontSize: "12px", overflowWrap: "anywhere" }}>{detail.base_sha || "全量快照"} → {detail.head_sha || detail.snapshot_id}</p>
                {detail.error_message ? <p style={{ color: "#721c24" }}>{detail.error_message}</p> : null}
                {updateError ? <p style={{ color: "#8f2f2f", background: "#f8e8e5", padding: "8px 10px", borderRadius: "8px", fontSize: "12px" }}>{updateError}</p> : null}
                <div style={{ display: "grid", gap: "8px", flex: "1 1 auto", minHeight: 0, overflow: "auto", paddingRight: "4px" }}>
                  {detail.items.map((item) => {
                    const display = getBusinessDocItemDisplay(item);
                    const tone = BUSINESS_DOC_ITEM_TONES[display.tone];
                    const reviewLabels = getBusinessDocReviewLabels(item);
                    const confidenceLabel = formatBusinessDocConfidence(item.confidence);
                    return (
                      <article key={item.item_id} style={{ borderTop: "1px solid var(--line-soft)", paddingTop: "10px" }}>
                        <strong style={{ fontSize: "13px", overflowWrap: "anywhere" }}>{item.source_path || "新文档候选分析"}</strong>
                        <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: "6px", marginTop: "7px" }}>
                          <span style={{ color: tone.color, background: tone.background, border: `1px solid ${tone.border}`, borderRadius: "999px", padding: "3px 8px", fontSize: "12px", fontWeight: 600, lineHeight: 1.4 }}>
                            {display.title}
                          </span>
                          {confidenceLabel ? <span style={{ color: "var(--text-faint)", fontSize: "12px" }}>{confidenceLabel}</span> : null}
                        </div>
                        <p style={{ color: "var(--text-soft)", fontSize: "12px", lineHeight: 1.55, margin: "6px 0 0" }}>{display.description}</p>
                        {reviewLabels.length ? <div style={{ color: "var(--text-faint)", fontSize: "12px", marginTop: "4px" }}>{reviewLabels.join(" · ")}</div> : null}
                        {item.review_feedback ? <div style={{ color: "var(--text-soft)", fontSize: "12px", marginTop: "4px" }}>{item.review_feedback}</div> : null}
                        {item.evidence?.length ? (
                          <div style={{ marginTop: "8px" }}>
                            <div style={{ color: "var(--text-soft)", fontSize: "11px", fontWeight: 600 }}>判断依据</div>
                            <ul style={{ margin: "4px 0 0", paddingLeft: "18px", color: "var(--text-faint)", fontSize: "11px" }}>
                              {item.evidence.map((evidence, index) => (
                                <li key={`${evidence.repo_key || "repo"}-${evidence.file || "file"}-${evidence.symbol || "symbol"}-${index}`} style={{ overflowWrap: "anywhere", marginTop: index ? "5px" : 0 }}>
                                  {evidence.description ? <span style={{ display: "block", color: "var(--text-soft)", lineHeight: 1.5 }}>{evidence.description}</span> : null}
                                  <span>{evidence.repo_key} · {String(evidence.commit || "").slice(0, 10)} · {evidence.file}{evidence.symbol ? ` · ${evidence.symbol}` : ""}</span>
                                </li>
                              ))}
                            </ul>
                          </div>
                        ) : null}
                        {item.update_id && item.apply_status === "manual_required" ? (
                          <button className="button button-secondary" style={{ fontSize: "12px", padding: "5px 10px", marginTop: "9px" }} disabled={batchBusy} onClick={() => onOpenUpdate(item.update_id)}>
                            查看并处理
                          </button>
                        ) : null}
                        {item.error_message ? <div style={{ color: "#8f2f2f", fontSize: "12px", marginTop: "6px" }}>错误原因：{item.error_message}</div> : null}
                      </article>
                    );
                  })}
                </div>
              </>
            ) : null}
          </aside>
        ) : null}
      </div>
      {!loading && !runs.length ? <p style={{ color: "var(--text-faint)", textAlign: "center", padding: "40px 0" }}>暂无运行记录</p> : null}
    </section>
  );
}

function ImageUsagePanel({ rows, loading, error, query, onQueryChange, sort, onSort, onRefresh }) {
  const q = (query || "").trim().toLowerCase();
  const filtered = q
    ? rows.filter(
        (r) => (r.name || "").toLowerCase().includes(q) || (r.email || "").toLowerCase().includes(q)
      )
    : rows;
  const sorted = [...filtered].sort((a, b) => {
    if (sort === "daily") return b.daily_used - a.daily_used || b.total_used - a.total_used;
    if (sort === "weekly") return b.weekly_used - a.weekly_used || b.total_used - a.total_used;
    if (sort === "email") return (a.email || "").localeCompare(b.email || "");
    return b.total_used - a.total_used || b.weekly_used - a.weekly_used;
  });

  const SortHead = ({ field, children }) => (
    <th
      style={{ padding: "10px 12px", textAlign: "right", cursor: "pointer", userSelect: "none" }}
      onClick={() => onSort(field)}
    >
      <span style={{ display: "inline-flex", alignItems: "center", gap: "4px" }}>
        {children}
        {sort === field ? <span aria-hidden="true">▾</span> : null}
      </span>
    </th>
  );

  return (
    <>
      <h2 style={{ margin: "0 0 16px", fontSize: "18px" }}>生图用量（每用户）</h2>
      <div style={{ display: "flex", gap: "8px", alignItems: "center", marginBottom: "16px" }}>
        <input
          value={query}
          onChange={(e) => onQueryChange(e.target.value)}
          placeholder="搜索姓名或邮箱"
          style={{
            flex: 1,
            padding: "8px 12px",
            border: "1px solid var(--line)",
            borderRadius: "8px",
            fontSize: "13px",
            background: "var(--surface)",
          }}
        />
        <button className="button button-secondary" style={{ fontSize: "13px" }} onClick={onRefresh}>
          刷新
        </button>
      </div>
      {error ? (
        <p style={{ color: "#721c24", background: "#f8d7da", padding: "8px 12px", borderRadius: "6px", marginBottom: "12px" }}>
          {error}
        </p>
      ) : null}
      {loading ? (
        <p style={{ color: "var(--text-faint)" }}>加载中…</p>
      ) : sorted.length === 0 ? (
        <p style={{ color: "var(--text-faint)", textAlign: "center", padding: "40px 0" }}>暂无数据</p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "14px" }}>
          <thead>
            <tr style={{ borderBottom: "2px solid var(--line)", textAlign: "left" }}>
              <th style={{ padding: "10px 12px" }}>用户</th>
              <th style={{ padding: "10px 12px" }}>状态</th>
              <SortHead field="daily">今日</SortHead>
              <SortHead field="weekly">本周</SortHead>
              <SortHead field="total">合计</SortHead>
            </tr>
          </thead>
          <tbody>
            {sorted.map((r) => (
              <tr key={r.user_id} style={{ borderBottom: "1px solid var(--line-soft)" }}>
                <td style={{ padding: "10px 12px" }}>
                  <div style={{ fontWeight: 500 }}>{r.name || "（未设置）"}</div>
                  <div style={{ color: "var(--text-faint)", fontSize: "12px" }}>{r.email}</div>
                </td>
                <td style={{ padding: "10px 12px", color: "var(--text-soft)" }}>{r.status}</td>
                <td style={{ padding: "10px 12px", textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{r.daily_used}</td>
                <td style={{ padding: "10px 12px", textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{r.weekly_used}</td>
                <td style={{ padding: "10px 12px", textAlign: "right", fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>
                  {r.total_used}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p style={{ marginTop: "12px", fontSize: "12px", color: "var(--text-faint)" }}>
        统计口径：成功落盘的生图；今日按 UTC 自然日、本周按 UTC 自然周（周一起）。点击表头可排序。
      </p>
    </>
  );
}

async function listChatSessions({ query, userId, limit = 50, offset = 0 } = {}) {
  const search = new URLSearchParams();
  if (query) {
    search.set("query", query);
  }
  if (userId) {
    search.set("user_id", userId);
  }
  search.set("limit", String(limit));
  search.set("offset", String(offset));
  return apiFetch(`/assistant/sessions?${search.toString()}`);
}

async function getChatSession(sessionId) {
  return apiFetch(`/assistant/sessions/${sessionId}`);
}

async function blockUser(userId) {
  return apiFetch(`/users/${userId}/block`, { method: "POST" });
}

async function setUserRole(userId, role) {
  return apiFetch(`/users/${userId}/role`, {
    method: "PATCH",
    body: JSON.stringify({ role }),
  });
}

async function setUserAgentAccess(userId, agentAccess) {
  return apiFetch(`/users/${userId}/agent-access`, {
    method: "POST",
    body: JSON.stringify({ agent_access: agentAccess }),
  });
}

async function listAgentAccessRequests(status = "pending") {
  // 后端 status 参数默认 pending；要看全部需显式传空值
  return apiFetch(`/agent-access/requests?status=${encodeURIComponent(status || "")}`);
}

async function reviewAgentAccessRequest(requestId, action, reviewComment = "") {
  return apiFetch(`/agent-access/requests/${requestId}/${action}`, {
    method: "POST",
    body: JSON.stringify({ review_comment: reviewComment || null }),
  });
}

async function listOrganizations() {
  return apiFetch("/organizations");
}

async function listPermissionDepartments() {
  return apiFetch("/permissions/departments");
}

async function listPermissionPolicies() {
  return apiFetch("/permissions/policies");
}

async function savePermissionPolicy(policy, policyId = null) {
  return apiFetch(policyId ? `/permissions/policies/${encodeURIComponent(policyId)}` : "/permissions/policies", {
    method: policyId ? "PATCH" : "POST",
    body: JSON.stringify(policy),
  });
}

async function setPermissionPolicyStatus(policyId, status) {
  return apiFetch(`/permissions/policies/${encodeURIComponent(policyId)}/status`, {
    method: "PATCH",
    body: JSON.stringify({ status }),
  });
}

async function deletePermissionPolicy(policyId) {
  return apiFetch(`/permissions/policies/${encodeURIComponent(policyId)}`, { method: "DELETE" });
}

async function listPermissionPolicyAudit(policyId) {
  return apiFetch(`/permissions/policies/${encodeURIComponent(policyId)}/audit`);
}

async function checkPermissionUser(userId, organizationKey) {
  const search = new URLSearchParams({ user_id: userId, organization_key: organizationKey });
  return apiFetch(`/permissions/check?${search.toString()}`);
}

async function listUserOrganizationMemberships(userId) {
  return apiFetch(`/users/${userId}/organization-memberships`);
}

async function assignUserOrganization(userId, payload) {
  return apiFetch(`/users/${userId}/organization-memberships`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

async function updateUserOrganizationMembership(userId, membershipId, payload) {
  return apiFetch(`/users/${userId}/organization-memberships/${membershipId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

async function listOrganizationAccessRequests(status = "pending") {
  const search = status ? `?status=${encodeURIComponent(status)}` : "";
  return apiFetch(`/organization-access-requests${search}`);
}

async function reviewOrganizationAccessRequest(requestId, action, reviewComment = "") {
  return apiFetch(`/organization-access-requests/${requestId}/${action}`, {
    method: "POST",
    body: JSON.stringify({ review_comment: reviewComment || null }),
  });
}

async function adminLogout() {
  return apiFetch("/auth/logout", { method: "POST" });
}

const STATUS_LABELS = { active: "正常", blocked: "已封禁" };
const AGENT_ACCESS_LABELS = { none: "未开通", pending: "审核中", active: "已开通", rejected: "已拒绝" };
const ROLE_LABELS = { member: "普通用户", admin: "管理员" };
const chatSessionPageSize = 30;
const chatSessionPollIntervalMs = 5000;

function formatDateTime(value) {
  if (!value) return "暂无";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "暂无";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function previewText(value, limit = 80) {
  const normalized = (value || "").replace(/\s+/g, " ").trim();
  if (!normalized) return "暂无内容";
  return normalized.length > limit ? `${normalized.slice(0, limit)}...` : normalized;
}

function userLabel(user) {
  return user?.name || user?.email || user?.user_id || "未知用户";
}

function sortUsersAlphabetically(users) {
  return [...users].sort((a, b) => {
    const aLabel = userLabel(a).toLocaleLowerCase("zh-CN");
    const bLabel = userLabel(b).toLocaleLowerCase("zh-CN");
    return aLabel.localeCompare(bLabel, "zh-CN");
  });
}

function StatusBadge({ status }) {
  const colors = {
    active: { background: "#d4edda", color: "#155724" },
    blocked: { background: "#f8d7da", color: "#721c24" },
  };
  const style = {
    display: "inline-block",
    padding: "2px 8px",
    borderRadius: "12px",
    fontSize: "12px",
    fontWeight: 600,
    ...(colors[status] || {}),
  };
  return <span style={style}>{STATUS_LABELS[status] || status}</span>;
}

function AgentAccessBadge({ agentAccess }) {
  const colors = {
    none: { background: "#f0eee6", color: "#5e5d59" },
    pending: { background: "#fef3cd", color: "#856404" },
    active: { background: "#d4edda", color: "#155724" },
    rejected: { background: "#f8d7da", color: "#721c24" },
  };
  const style = {
    display: "inline-block",
    padding: "2px 8px",
    borderRadius: "12px",
    fontSize: "12px",
    fontWeight: 600,
    ...(colors[agentAccess] || {}),
  };
  return <span style={style}>{AGENT_ACCESS_LABELS[agentAccess] || agentAccess}</span>;
}

function formatUserOrganizations(organizations = []) {
  const activeOrganizations = organizations.filter((item) => item?.organization_key);
  if (!activeOrganizations.length) return "未启用";
  return activeOrganizations
    .map((item) => `${item.organization_key}${item.is_default ? "（默认）" : ""}`)
    .join("、");
}

// 部门完整路径为「一级 / 二级 / 三级」，列表默认展示第二级，悬停 title 展示完整路径
function shortDepartmentName(fullPath) {
  if (!fullPath) return "";
  const segments = fullPath.split(" / ");
  return segments.length > 1 ? segments[1] : segments[0];
}

// 用工形式列：OA 目录不可用时「未获取」；在目录中显示全职/兼职/实习/未填写；公司域名不在目录中为「离职」
function formatEmploymentForm(user) {
  if (!user.oa_directory_loaded) return { label: "未获取", color: "var(--text-faint)" };
  if (!user.employment_form) return { label: "离职", color: "#721c24" };
  return { label: user.employment_form, color: "var(--text)" };
}

const userTableExtraStyles = `
  .admin-clickable { cursor: pointer; }
  .admin-clickable:hover { text-decoration: underline dashed; text-underline-offset: 3px; }
  .admin-menu-item {
    appearance: none; border: none; background: transparent; width: 100%;
    text-align: left; font-size: 13px; padding: 7px 10px; border-radius: 6px;
    cursor: pointer; font-family: var(--font-sans); color: var(--text);
  }
  .admin-menu-item:hover { background: var(--surface-muted); }
`;

// 状态标签点击后的自定义操作菜单，按当前状态给出可用动作
function ActionMenu({ items, onClose }) {
  if (!items.length) {
    return null;
  }
  return (
    <>
      <div style={{ position: "fixed", inset: 0, zIndex: 40 }} onClick={onClose} />
      <div
        style={{
          position: "absolute",
          top: "calc(100% + 6px)",
          left: 0,
          zIndex: 41,
          minWidth: "128px",
          background: "var(--surface-strong)",
          border: "1px solid var(--line)",
          borderRadius: "8px",
          boxShadow: "var(--shadow-large)",
          padding: "4px",
          display: "flex",
          flexDirection: "column",
          gap: "2px",
        }}
      >
        {items.map((item) => (
          <button
            key={item.label}
            className="admin-menu-item"
            style={item.danger ? { color: "#721c24" } : undefined}
            onClick={() => {
              onClose();
              item.act();
            }}
          >
            {item.label}
          </button>
        ))}
      </div>
    </>
  );
}

// 账号状态操作：注册即正常，唯一动作是封禁
function StatusActionMenu({ user, isCurrentAdmin, onBlock, onClose }) {
  const items = [];
  if (user.status !== "blocked" && !isCurrentAdmin) {
    items.push({ label: "封禁账号", danger: true, act: () => onBlock(user.user_id) });
  }
  return <ActionMenu items={items} onClose={onClose} />;
}

// Agent 权限操作：待审核的申请在「Agent 审核」队列处理，这里只做直接开通/收回
function AgentAccessActionMenu({ user, onSetAgentAccess, onClose }) {
  const items = [];
  if (user.agent_access === "active") {
    items.push({ label: "收回 Agent 权限", danger: true, act: () => onSetAgentAccess(user.user_id, "none") });
  } else if (user.agent_access === "none" || user.agent_access === "rejected") {
    items.push({ label: "直接开通 Agent", danger: false, act: () => onSetAgentAccess(user.user_id, "active") });
  }
  return <ActionMenu items={items} onClose={onClose} />;
}

function UserTable({
  users,
  onBlock,
  onSetAgentAccess,
  onSetRole,
  onManageOrganizations,
  currentUserId,
  selectedOrganizationUser,
  organizations,
  memberships,
  accessRequests,
  organizationPanelLoading,
  organizationPanelError,
  onCloseOrganizationPanel,
  onAssignOrganization,
  onUpdateMembership,
  onApproveOrganizationRequest,
  onRejectOrganizationRequest,
}) {
  const [statusMenuUserId, setStatusMenuUserId] = useState(null);
  const [agentMenuUserId, setAgentMenuUserId] = useState(null);

  if (!users.length) {
    return <p style={{ color: "var(--text-faint)", textAlign: "center", padding: "40px 0" }}>暂无用户</p>;
  }
  return (
    <>
    <style>{userTableExtraStyles}</style>
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "14px" }}>
      <thead>
        <tr style={{ borderBottom: "2px solid var(--line)", textAlign: "left" }}>
          <th style={{ padding: "10px 12px" }}>用户</th>
          <th style={{ padding: "10px 12px", whiteSpace: "nowrap" }}>状态</th>
          <th style={{ padding: "10px 12px", whiteSpace: "nowrap" }}>Agent 权限</th>
          <th style={{ padding: "10px 12px" }}>角色</th>
          <th style={{ padding: "10px 12px" }}>部门</th>
          <th style={{ padding: "10px 12px", whiteSpace: "nowrap" }}>用工形式</th>
          <th style={{ padding: "10px 12px" }}>启用组织</th>
          <th style={{ padding: "10px 12px", whiteSpace: "nowrap" }}>注册时间</th>
        </tr>
      </thead>
      <tbody>
        {users.map((u) => (
          <Fragment key={u.user_id}>
            <tr style={{ borderBottom: "1px solid var(--line-soft)" }}>
              <td style={{ padding: "10px 12px" }}>
                <div style={{ fontWeight: 500 }}>{u.name || "（未设置）"}</div>
                <div style={{ color: "var(--text-faint)", fontSize: "12px" }}>{u.email}</div>
              </td>
              <td style={{ padding: "10px 12px", whiteSpace: "nowrap", position: "relative" }}>
                <span
                  className="admin-clickable"
                  title="点击处理状态"
                  onClick={() => setStatusMenuUserId((current) => (current === u.user_id ? null : u.user_id))}
                >
                  <StatusBadge status={u.status} />
                </span>
                {statusMenuUserId === u.user_id ? (
                  <StatusActionMenu
                    user={u}
                    isCurrentAdmin={u.user_id === currentUserId}
                    onBlock={onBlock}
                    onClose={() => setStatusMenuUserId(null)}
                  />
                ) : null}
              </td>
              <td style={{ padding: "10px 12px", whiteSpace: "nowrap", position: "relative" }}>
                <span
                  className="admin-clickable"
                  title={u.agent_access === "pending" ? "待审核的申请请在「Agent 审核」中处理" : "点击调整 Agent 权限"}
                  onClick={() => setAgentMenuUserId((current) => (current === u.user_id ? null : u.user_id))}
                >
                  <AgentAccessBadge agentAccess={u.agent_access} />
                </span>
                {agentMenuUserId === u.user_id ? (
                  <AgentAccessActionMenu
                    user={u}
                    onSetAgentAccess={onSetAgentAccess}
                    onClose={() => setAgentMenuUserId(null)}
                  />
                ) : null}
              </td>
              <td style={{ padding: "10px 12px", whiteSpace: "nowrap" }}>
                {u.user_id === currentUserId ? (
                  <span>{ROLE_LABELS[u.role] || u.role}</span>
                ) : (
                  <span
                    className="admin-clickable"
                    title={u.role === "admin" ? "点击取消管理员" : "点击设为管理员"}
                    onClick={() => onSetRole(u.user_id, u.role === "admin" ? "member" : "admin")}
                  >
                    {ROLE_LABELS[u.role] || u.role}
                  </span>
                )}
              </td>
              <td
                style={{ padding: "10px 12px", color: u.department_name ? "var(--text)" : "var(--text-faint)", whiteSpace: "nowrap" }}
                title={u.department_name || undefined}
              >
                {shortDepartmentName(u.department_name) || "未获取"}
              </td>
              <td style={{ padding: "10px 12px", color: formatEmploymentForm(u).color, whiteSpace: "nowrap" }}>
                {formatEmploymentForm(u).label}
              </td>
              <td style={{ padding: "10px 12px" }}>
                <span
                  className="admin-clickable"
                  title="点击配置启用组织"
                  onClick={() => onManageOrganizations(u)}
                  style={{ color: u.organizations?.length ? "var(--text)" : "var(--text-faint)" }}
                >
                  {formatUserOrganizations(u.organizations)}
                </span>
              </td>
              <td style={{ padding: "10px 12px", color: "var(--text-soft)", whiteSpace: "nowrap" }}>
                {new Date(u.created_at).toLocaleDateString("zh-CN")}
              </td>
            </tr>
            {selectedOrganizationUser?.user_id === u.user_id ? (
              <tr style={{ borderBottom: "1px solid var(--line-soft)" }}>
                <td colSpan="8" style={{ padding: "0 12px 16px" }}>
                  <OrganizationPanel
                    user={u}
                    organizations={organizations}
                    memberships={memberships}
                    accessRequests={accessRequests}
                    loading={organizationPanelLoading}
                    error={organizationPanelError}
                    onClose={onCloseOrganizationPanel}
                    onAssign={onAssignOrganization}
                    onUpdateMembership={onUpdateMembership}
                    onApproveRequest={onApproveOrganizationRequest}
                    onRejectRequest={onRejectOrganizationRequest}
                  />
                </td>
              </tr>
            ) : null}
          </Fragment>
        ))}
      </tbody>
    </table>
    </>
  );
}

// Agent 使用权限申请审核队列
function AgentReviewPanel({ requests, filter, loading, error, onFilterChange, onReview }) {
  const filterTabs = [
    { label: "待审核", value: "pending" },
    { label: "已通过", value: "approved" },
    { label: "已拒绝", value: "rejected" },
    { label: "全部", value: "" },
  ];
  const statusLabels = { pending: "待审核", approved: "已通过", rejected: "已拒绝" };
  return (
    <>
      <h2 style={{ margin: "0 0 20px", fontSize: "18px" }}>Agent 审核</h2>

      <div style={{ display: "flex", gap: "8px", marginBottom: "16px" }}>
        {filterTabs.map((t) => (
          <button
            key={t.value}
            className={`button ${filter === t.value ? "button-primary" : "button-secondary"}`}
            style={{ fontSize: "13px" }}
            onClick={() => onFilterChange(t.value)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {error && (
        <p style={{ color: "#721c24", background: "#f8d7da", padding: "8px 12px", borderRadius: "6px", marginBottom: "12px" }}>
          {error}
        </p>
      )}

      {loading ? (
        <p style={{ color: "var(--text-faint)" }}>加载中…</p>
      ) : !requests.length ? (
        <p style={{ color: "var(--text-faint)", textAlign: "center", padding: "40px 0" }}>暂无申请</p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "14px" }}>
          <thead>
            <tr style={{ borderBottom: "2px solid var(--line)", textAlign: "left" }}>
              <th style={{ padding: "10px 12px" }}>申请人</th>
              <th style={{ padding: "10px 12px" }}>申请原因</th>
              <th style={{ padding: "10px 12px", whiteSpace: "nowrap" }}>申请时间</th>
              <th style={{ padding: "10px 12px", whiteSpace: "nowrap" }}>状态</th>
            </tr>
          </thead>
          <tbody>
            {requests.map((item) => (
              <tr key={item.request_id} style={{ borderBottom: "1px solid var(--line-soft)" }}>
                <td style={{ padding: "10px 12px" }}>
                  <div style={{ fontWeight: 500 }}>{item.user_name || "（未设置）"}</div>
                  <div style={{ color: "var(--text-faint)", fontSize: "12px" }}>{item.user_email}</div>
                </td>
                <td style={{ padding: "10px 12px", color: "var(--text-soft)" }}>{item.reason}</td>
                <td style={{ padding: "10px 12px", color: "var(--text-soft)", whiteSpace: "nowrap" }}>
                  {formatDateTime(item.created_at)}
                </td>
                <td style={{ padding: "10px 12px", whiteSpace: "nowrap" }}>
                  {item.status === "pending" ? (
                    <span style={{ display: "inline-flex", gap: "6px" }}>
                      <button
                        className="button button-primary"
                        style={{ fontSize: "12px", padding: "3px 8px" }}
                        onClick={() => onReview(item.request_id, "approve")}
                      >
                        通过
                      </button>
                      <button
                        className="button button-secondary"
                        style={{ fontSize: "12px", padding: "3px 8px" }}
                        onClick={() => onReview(item.request_id, "reject")}
                      >
                        拒绝
                      </button>
                    </span>
                  ) : (
                    <span style={{ color: "var(--text-soft)" }}>
                      {statusLabels[item.status] || item.status}
                      {item.review_comment ? `（${item.review_comment}）` : ""}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function OrganizationPanel({
  user,
  organizations,
  memberships,
  accessRequests,
  loading,
  error,
  onClose,
  onAssign,
  onUpdateMembership,
  onApproveRequest,
  onRejectRequest,
}) {
  if (!user) {
    return null;
  }

  const membershipsByOrganization = new Map(memberships.map((item) => [item.organization_key, item]));

  return (
    <section style={{ border: "1px solid var(--line)", borderRadius: "8px", background: "var(--surface-strong)", padding: "16px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: "12px", alignItems: "flex-start", marginBottom: "14px" }}>
        <div>
          <h3 style={{ margin: "0 0 4px", fontSize: "16px" }}>组织访问</h3>
          <p style={{ margin: 0, color: "var(--text-soft)", fontSize: "13px" }}>
            {user.name || user.email} · {user.email}
          </p>
        </div>
        <button className="button button-secondary" style={{ fontSize: "12px" }} onClick={onClose}>
          关闭
        </button>
      </div>

      {error ? <p style={{ color: "#721c24", margin: "0 0 12px" }}>{error}</p> : null}
      {loading ? <p style={{ color: "var(--text-faint)" }}>加载组织配置中...</p> : null}

      {!loading ? (
        <>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "13px", marginBottom: "16px" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--line)" }}>
                <th style={{ textAlign: "left", padding: "8px" }}>启用访问</th>
                <th style={{ textAlign: "left", padding: "8px" }}>组织</th>
                <th style={{ textAlign: "left", padding: "8px" }}>默认</th>
                <th style={{ textAlign: "left", padding: "8px" }}>角色</th>
                <th style={{ textAlign: "left", padding: "8px" }}>来源</th>
              </tr>
            </thead>
            <tbody>
              {organizations.map((organization) => {
                const membership = membershipsByOrganization.get(organization.organization_key);
                const active = membership?.status === "active";
                return (
                  <tr key={organization.organization_key} style={{ borderBottom: "1px solid var(--line-soft)" }}>
                    <td style={{ padding: "8px" }}>
                      <input
                        type="checkbox"
                        checked={active}
                        onChange={(event) => {
                          if (membership) {
                            onUpdateMembership(membership, {
                              status: event.target.checked ? "active" : "disabled",
                              is_default: event.target.checked ? membership.is_default : false,
                            });
                          } else if (event.target.checked) {
                            onAssign(organization.organization_key, false);
                          }
                        }}
                      />
                    </td>
                    <td style={{ padding: "8px" }}>{organization.display_name || organization.organization_key}</td>
                    <td style={{ padding: "8px" }}>
                      <input
                        type="radio"
                        name={`default-organization-${user.user_id}`}
                        checked={active && Boolean(membership?.is_default)}
                        disabled={!active || !membership}
                        onChange={() => {
                          if (membership) {
                            onUpdateMembership(membership, { is_default: true });
                          }
                        }}
                      />
                    </td>
                    <td style={{ padding: "8px" }}>{membership?.organization_role || "-"}</td>
                    <td style={{ padding: "8px", color: membership ? "var(--text-soft)" : "var(--text-faint)" }}>{membership?.source || "-"}</td>
                  </tr>
                );
              })}
              {!organizations.length ? (
                <tr>
                  <td colSpan="5" style={{ padding: "12px", color: "var(--text-faint)", textAlign: "center" }}>暂无可分配组织</td>
                </tr>
              ) : null}
            </tbody>
          </table>

          <h4 style={{ margin: "0 0 8px", fontSize: "14px" }}>待审批跨组织申请</h4>
          {accessRequests.length ? (
            <div style={{ display: "grid", gap: "8px" }}>
              {accessRequests.map((request) => (
                <div key={request.request_id} style={{ border: "1px solid var(--line-soft)", borderRadius: "6px", padding: "10px" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", gap: "12px" }}>
                    <strong>{request.target_organization_key}</strong>
                    <span style={{ color: "var(--text-faint)", fontSize: "12px" }}>{request.status}</span>
                  </div>
                  <p style={{ margin: "6px 0", color: "var(--text-soft)", fontSize: "13px" }}>{request.reason}</p>
                  <div style={{ display: "flex", gap: "8px" }}>
                    <button className="button button-primary" style={{ fontSize: "12px", padding: "3px 8px" }} onClick={() => onApproveRequest(request.request_id)}>
                      通过
                    </button>
                    <button className="button button-secondary" style={{ fontSize: "12px", padding: "3px 8px" }} onClick={() => onRejectRequest(request.request_id)}>
                      拒绝
                    </button>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p style={{ margin: 0, color: "var(--text-faint)", fontSize: "13px" }}>暂无待审批申请</p>
          )}
        </>
      ) : null}
    </section>
  );
}

function ChatLogsPanel({
  sessions,
  selectedSessionId,
  detail,
  query,
  users,
  selectedUserId,
  userFilterInput,
  userPickerOpen,
  usersLoading,
  page,
  pageSize,
  hasNextPage,
  loading,
  detailLoading,
  error,
  onQueryChange,
  onUserFilterInputChange,
  onOpenUserPicker,
  onToggleUserPicker,
  onSelectUser,
  onSelectSession,
  onPreviousPage,
  onNextPage,
  onRefresh,
  onBack,
}) {
  const selectedSession = detail?.session || sessions.find((session) => session.session_id === selectedSessionId);
  const selectedUser = users.find((user) => user.user_id === selectedUserId);
  const normalizedUserFilter = userFilterInput.trim().toLocaleLowerCase("zh-CN");
  const filteredUsers = normalizedUserFilter
    ? users.filter((user) => {
        const label = userLabel(user).toLocaleLowerCase("zh-CN");
        const email = (user.email || "").toLocaleLowerCase("zh-CN");
        return label.includes(normalizedUserFilter) || email.includes(normalizedUserFilter);
      })
    : users;

  return (
    <div style={{ display: "grid", gridTemplateRows: "auto minmax(0, 1fr)", gap: "16px", minHeight: "calc(100vh - 104px)" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "16px" }}>
        <div>
          <h2 style={{ margin: "0 0 6px", fontSize: "18px" }}>聊天记录运维</h2>
          <p style={{ margin: 0, color: "var(--text-soft)", fontSize: "13px" }}>仅管理员可见，用于临时运维排查。</p>
        </div>
        <button className="button button-secondary" style={{ fontSize: "13px" }} onClick={onBack}>
          返回用户管理
        </button>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "360px minmax(0, 1fr)", gap: "16px", minHeight: 0 }}>
        <aside
          style={{
            minHeight: 0,
            border: "1px solid var(--line)",
            borderRadius: "8px",
            background: "var(--surface-strong)",
            overflow: "hidden",
            display: "grid",
            gridTemplateRows: "auto minmax(0, 1fr) auto",
          }}
        >
          <div style={{ display: "grid", gap: "8px", padding: "12px", borderBottom: "1px solid var(--line-soft)" }}>
            <div style={{ display: "flex", gap: "8px" }}>
              <input
                value={query}
                onChange={(event) => onQueryChange(event.target.value)}
                placeholder="搜索标题或消息内容"
                style={{
                  minWidth: 0,
                  flex: 1,
                  height: "36px",
                  border: "1px solid var(--line)",
                  borderRadius: "8px",
                  padding: "0 10px",
                  background: "var(--bg-soft)",
                  color: "var(--text)",
                }}
              />
              <button
                type="button"
                className="button button-secondary"
                style={{ flex: "0 0 auto", fontSize: "12px", padding: "0 12px" }}
                onClick={onRefresh}
                disabled={loading}
              >
                刷新
              </button>
            </div>
            <div style={{ position: "relative" }}>
              <input
                value={userFilterInput}
                onFocus={onOpenUserPicker}
                onChange={(event) => onUserFilterInputChange(event.target.value)}
                placeholder={selectedUser ? userLabel(selectedUser) : "按用户筛选"}
                style={{
                  width: "100%",
                  height: "36px",
                  border: "1px solid var(--line)",
                  borderRadius: "8px",
                  padding: "0 58px 0 10px",
                  background: "var(--bg-soft)",
                  color: "var(--text)",
                }}
              />
              <button
                type="button"
                onClick={onToggleUserPicker}
                style={{
                  position: "absolute",
                  top: "4px",
                  right: "4px",
                  height: "28px",
                  padding: "0 8px",
                  borderRadius: "6px",
                  background: "var(--surface-muted)",
                  color: "var(--text-soft)",
                  fontSize: "12px",
                }}
              >
                {userPickerOpen ? "收起" : "选择"}
              </button>
              {userPickerOpen ? (
                <div
                  style={{
                    position: "absolute",
                    zIndex: 5,
                    top: "42px",
                    left: 0,
                    right: 0,
                    maxHeight: "280px",
                    overflow: "auto",
                    border: "1px solid var(--line)",
                    borderRadius: "8px",
                    background: "var(--surface-strong)",
                    boxShadow: "var(--shadow-large)",
                  }}
                >
                  <button
                    style={{
                      width: "100%",
                      display: "block",
                      textAlign: "left",
                      padding: "10px 12px",
                      border: 0,
                      borderBottom: "1px solid var(--line-soft)",
                      borderRadius: 0,
                      color: "var(--text)",
                      background: selectedUserId ? "transparent" : "var(--surface-muted)",
                    }}
                    onClick={() => onSelectUser("")}
                  >
                    全部用户
                  </button>
                  {usersLoading ? <p style={{ color: "var(--text-faint)", padding: "12px", margin: 0 }}>加载用户中...</p> : null}
                  {!usersLoading && filteredUsers.map((user) => (
                    <button
                      key={user.user_id}
                      style={{
                        width: "100%",
                        display: "block",
                        textAlign: "left",
                        padding: "10px 12px",
                        border: 0,
                        borderBottom: "1px solid var(--line-soft)",
                        borderRadius: 0,
                        color: "var(--text)",
                        background: user.user_id === selectedUserId ? "var(--surface-muted)" : "transparent",
                      }}
                      onClick={() => onSelectUser(user.user_id)}
                    >
                      <div style={{ fontSize: "13px", fontWeight: 600, overflowWrap: "anywhere" }}>{userLabel(user)}</div>
                      <div style={{ marginTop: "3px", fontSize: "12px", color: "var(--text-faint)", overflowWrap: "anywhere" }}>{user.email}</div>
                    </button>
                  ))}
                  {!usersLoading && !filteredUsers.length ? (
                    <p style={{ color: "var(--text-faint)", padding: "12px", margin: 0 }}>没有匹配用户</p>
                  ) : null}
                </div>
              ) : null}
            </div>
          </div>

          <div style={{ overflow: "auto" }}>
            {loading ? <p style={{ color: "var(--text-faint)", padding: "20px 12px", margin: 0 }}>加载中...</p> : null}
            {!loading && !sessions.length ? <p style={{ color: "var(--text-faint)", padding: "20px 12px", margin: 0 }}>暂无聊天记录</p> : null}
            {sessions.map((session) => {
              const active = session.session_id === selectedSessionId;
              return (
                <button
                  key={session.session_id}
                  onClick={() => onSelectSession(session.session_id)}
                  style={{
                    width: "100%",
                    display: "block",
                    textAlign: "left",
                    padding: "12px",
                    border: 0,
                    borderBottom: "1px solid var(--line-soft)",
                    borderRadius: 0,
                    background: active ? "var(--surface-muted)" : "transparent",
                    color: "var(--text)",
                  }}
                >
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "8px" }}>
                    <strong style={{ display: "inline-flex", alignItems: "center", gap: "6px", minWidth: 0, fontSize: "13px", overflowWrap: "anywhere" }}>
                      <span>{session.title || "未命名会话"}</span>
                      {session.has_active_turn ? (
                        <span className="admin-chat-running-indicator" aria-label="正在进行" title="正在进行">
                          <span className="admin-chat-running-spinner" aria-hidden="true" />
                        </span>
                      ) : null}
                    </strong>
                    <span style={{ flex: "0 0 auto", fontSize: "12px", color: "var(--text-faint)" }}>{session.message_count} 条</span>
                  </div>
                  <div style={{ marginTop: "4px", fontSize: "12px", color: "var(--text-soft)", overflowWrap: "anywhere" }}>
                    {session.owner.name || session.owner.email || session.owner.user_id}
                  </div>
                  <div style={{ marginTop: "6px", fontSize: "12px", color: "var(--text-faint)", lineHeight: 1.45 }}>
                    {previewText(session.last_message_preview, 58)}
                  </div>
                  <div style={{ marginTop: "6px", fontSize: "11px", color: "var(--text-faint)" }}>{formatDateTime(session.updated_at)}</div>
                </button>
              );
            })}
          </div>

          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: "8px",
              padding: "10px 12px",
              borderTop: "1px solid var(--line-soft)",
              background: "var(--surface-strong)",
            }}
          >
            <span style={{ color: "var(--text-soft)", fontSize: "12px" }}>第 {page + 1} 页 · 每页 {pageSize} 条</span>
            <div style={{ display: "flex", gap: "6px" }}>
              <button
                className="button button-secondary"
                style={{ fontSize: "12px", padding: "4px 10px" }}
                onClick={onPreviousPage}
                disabled={page === 0 || loading}
              >
                上一页
              </button>
              <button
                className="button button-secondary"
                style={{ fontSize: "12px", padding: "4px 10px" }}
                onClick={onNextPage}
                disabled={!hasNextPage || loading}
              >
                下一页
              </button>
            </div>
          </div>
        </aside>

        <section
          style={{
            minHeight: 0,
            border: "1px solid var(--line)",
            borderRadius: "8px",
            background: "var(--surface-strong)",
            overflow: "hidden",
            display: "grid",
            gridTemplateRows: "auto minmax(0, 1fr)",
          }}
        >
          <div style={{ padding: "14px 16px", borderBottom: "1px solid var(--line-soft)" }}>
            {selectedSession ? (
              <>
                <h3 style={{ margin: "0 0 6px", fontSize: "16px", overflowWrap: "anywhere" }}>{selectedSession.title || "未命名会话"}</h3>
                <div style={{ display: "flex", gap: "12px", flexWrap: "wrap", color: "var(--text-soft)", fontSize: "12px" }}>
                  <span>{selectedSession.owner.name || selectedSession.owner.email || selectedSession.owner.user_id}</span>
                  <span>{selectedSession.message_count} 条消息</span>
                  <span>更新于 {formatDateTime(selectedSession.updated_at)}</span>
                  {selectedSession.runtime_provider ? <span>Runtime: {selectedSession.runtime_provider}</span> : null}
                </div>
              </>
            ) : (
              <h3 style={{ margin: 0, fontSize: "16px", color: "var(--text-soft)" }}>请选择会话</h3>
            )}
          </div>

          <div style={{ overflow: "auto", padding: "16px", background: "var(--bg-soft)" }}>
            {error ? (
              <p style={{ color: "#721c24", background: "#f8d7da", padding: "8px 12px", borderRadius: "6px", margin: 0 }}>{error}</p>
            ) : null}
            {detailLoading ? <p style={{ color: "var(--text-faint)", margin: 0 }}>加载消息中...</p> : null}
            {!detailLoading && detail?.messages?.length ? (
              <div style={{ display: "grid", gap: "12px" }}>
                {detail.messages.map((message) => {
                  const isAssistant = message.role === "assistant";
                  return (
                    <article
                      key={message.message_id}
                      style={{
                        border: "1px solid var(--line-soft)",
                        borderRadius: "8px",
                        background: isAssistant ? "var(--surface-strong)" : "var(--surface-user)",
                        padding: "12px",
                      }}
                    >
                      <MessageCard
                        message={toMessageCard(message, {
                          formatTime: formatDateTime,
                          imageUrlForId: (imageId) => `/api/admin/assistant/uploads/images/${encodeURIComponent(imageId)}`
                        })}
                      />
                    </article>
                  );
                })}
              </div>
            ) : null}
            {!detailLoading && detail && !detail.messages.length ? (
              <p style={{ color: "var(--text-faint)", margin: 0 }}>该会话暂无消息</p>
            ) : null}
          </div>
        </section>
      </div>
    </div>
  );
}

const EMPTY_PERMISSION_POLICY = {
  policy_name: "",
  organization_key: "",
  capability_key: "test_data",
  department_keys: [],
  include_children: true,
  task_scope_type: "all",
  tasks: [],
  status: "enabled",
  starts_at: "",
  expires_at: "",
  description: "",
};

function PermissionPolicyReadOnly({ policy, audit, onEdit, onDelete }) {
  return (
    <section style={{ border: "1px solid var(--line)", borderRadius: "10px", padding: "14px", background: "var(--surface-strong)", display: "grid", gap: "12px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "10px" }}>
        <div>
          <span style={{ display: "inline-block", color: "var(--text-soft)", background: "var(--surface-muted)", borderRadius: "999px", padding: "3px 8px", fontSize: "11px", fontWeight: 600, marginBottom: "5px" }}>查看模式</span>
          <h3 style={{ margin: 0, fontSize: "16px" }}>{policy.policy_name}</h3>
        </div>
        <div style={{ display: "flex", gap: "6px" }}>
          <button type="button" className="button button-primary" style={{ fontSize: "12px", padding: "5px 9px" }} onClick={onEdit}>编辑策略</button>
          <button type="button" className="button button-secondary" style={{ fontSize: "12px", padding: "5px 9px", color: "#8f2f2f" }} onClick={onDelete}>删除</button>
        </div>
      </div>
      <div style={{ display: "grid", gap: "9px", fontSize: "13px" }}>
        <div><span style={{ color: "var(--text-faint)" }}>组织：</span>{policy.organization_key}</div>
        <div><span style={{ color: "var(--text-faint)" }}>能力：</span>测试造数</div>
        <div><span style={{ color: "var(--text-faint)" }}>部门范围：</span>{(policy.departments || []).map((item) => `${item.department_path_snapshot}${item.include_children ? "（含子部门）" : ""}`).join("、") || "未配置"}</div>
        <div><span style={{ color: "var(--text-faint)" }}>授权范围：</span>UAT 环境控制（系统当前开放的环境和造数任务）</div>
        <div><span style={{ color: "var(--text-faint)" }}>状态：</span><strong style={{ color: policy.status === "enabled" ? "#3f684d" : "var(--text-faint)" }}>{policy.status === "enabled" ? "启用" : "停用"}</strong></div>
        <div><span style={{ color: "var(--text-faint)" }}>有效期：</span>{policy.starts_at || policy.expires_at ? `${policy.starts_at || "不限开始"} 至 ${policy.expires_at || "不限结束"}` : "长期有效"}</div>
        <div><span style={{ color: "var(--text-faint)" }}>说明：</span>{policy.description || "暂无说明"}</div>
        <div style={{ color: "var(--text-faint)", fontSize: "12px" }}>创建：{policy.created_by} · {formatDateTime(policy.created_at)}　最近修改：{policy.updated_by} · {formatDateTime(policy.updated_at)}</div>
      </div>
      {audit.length ? <div style={{ borderTop: "1px solid var(--line-soft)", paddingTop: "10px", color: "var(--text-faint)", fontSize: "11px" }}><strong>审计记录</strong>{audit.map((item) => <div key={item.audit_id} style={{ marginTop: "5px" }}>{item.action} · {item.actor_user_id} · {formatDateTime(item.created_at)}</div>)}</div> : null}
    </section>
  );
}

function PermissionPoliciesPanel() {
  const [policies, setPolicies] = useState([]);
  const [departments, setDepartments] = useState([]);
  const [users, setUsers] = useState([]);
  const [organizations, setOrganizations] = useState([]);
  const [form, setForm] = useState(EMPTY_PERMISSION_POLICY);
  const [panelMode, setPanelMode] = useState("empty");
  const [selectedPolicy, setSelectedPolicy] = useState(null);
  const [editingPolicyId, setEditingPolicyId] = useState(null);
  const [audit, setAudit] = useState([]);
  const [checkUserId, setCheckUserId] = useState("");
  const [checkOrganizationKey, setCheckOrganizationKey] = useState("");
  const [checkResult, setCheckResult] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function refresh() {
    setLoading(true);
    setError("");
    try {
      const [departmentPayload, nextPolicies, nextUsers, nextOrganizations] = await Promise.all([
        listPermissionDepartments(),
        listPermissionPolicies(),
        listUsers(null),
        listOrganizations(),
      ]);
      setDepartments(departmentPayload.departments || []);
      setPolicies(nextPolicies || []);
      setUsers(nextUsers || []);
      setOrganizations(nextOrganizations || []);
      setForm((current) => ({
        ...current,
        organization_key: current.organization_key || nextOrganizations?.[0]?.organization_key || "",
      }));
      setCheckUserId((current) => current || nextUsers?.[0]?.user_id || "");
      setCheckOrganizationKey((current) => current || nextOrganizations?.[0]?.organization_key || "");
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  function loadPolicyAudit(policyId) {
    setAudit([]);
    listPermissionPolicyAudit(policyId).then(setAudit).catch((e) => setError(e.message));
  }

  function viewPolicy(policy) {
    setSelectedPolicy(policy);
    setPanelMode("view");
    setEditingPolicyId(null);
    loadPolicyAudit(policy.policy_id);
  }

  function editPolicy(policy) {
    setSelectedPolicy(policy);
    setPanelMode("edit");
    setEditingPolicyId(policy.policy_id);
    setForm({
      policy_name: policy.policy_name,
      organization_key: policy.organization_key,
      capability_key: policy.capability_key,
      department_keys: (policy.departments || []).map((item) => item.department_key),
      include_children: Boolean(policy.departments?.[0]?.include_children),
      task_scope_type: "all",
      tasks: [],
      status: policy.status,
      starts_at: policy.starts_at ? policy.starts_at.slice(0, 16) : "",
      expires_at: policy.expires_at ? policy.expires_at.slice(0, 16) : "",
      description: policy.description || "",
    });
    loadPolicyAudit(policy.policy_id);
  }

  function beginCreatePolicy() {
    setSelectedPolicy(null);
    setPanelMode("edit");
    setEditingPolicyId(null);
    setAudit([]);
    setForm({ ...EMPTY_PERMISSION_POLICY, organization_key: organizations[0]?.organization_key || "" });
  }

  function resetForm() {
    setPanelMode("empty");
    setSelectedPolicy(null);
    setEditingPolicyId(null);
    setAudit([]);
    setForm({ ...EMPTY_PERMISSION_POLICY, organization_key: organizations[0]?.organization_key || "" });
  }

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const savedPolicy = await savePermissionPolicy({
        ...form,
        department_keys: form.department_keys,
        task_scope_type: "all",
        tasks: [],
        starts_at: form.starts_at || null,
        expires_at: form.expires_at || null,
        description: form.description || null,
      }, editingPolicyId);
      await refresh();
      setSelectedPolicy(savedPolicy);
      setPanelMode("view");
      setEditingPolicyId(null);
      loadPolicyAudit(savedPolicy.policy_id);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function checkUser(event) {
    event.preventDefault();
    if (!checkUserId || !checkOrganizationKey) return;
    setBusy(true);
    setError("");
    try {
      setCheckResult(await checkPermissionUser(checkUserId, checkOrganizationKey));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function togglePolicy(policy) {
    setBusy(true);
    setError("");
    try {
      const nextStatus = policy.status === "enabled" ? "disabled" : "enabled";
      await setPermissionPolicyStatus(policy.policy_id, nextStatus);
      await refresh();
      if (editingPolicyId === policy.policy_id) {
        setForm((current) => ({ ...current, status: nextStatus }));
      }
      if (selectedPolicy?.policy_id === policy.policy_id) {
        setSelectedPolicy((current) => ({ ...current, status: nextStatus, updated_at: new Date().toISOString() }));
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function removePolicy(policy) {
    if (!window.confirm(`确定删除策略“${policy.policy_name}”吗？删除后将保留审计记录，但不再参与权限判断。`)) return;
    setBusy(true);
    setError("");
    try {
      await deletePermissionPolicy(policy.policy_id);
      if (editingPolicyId === policy.policy_id || selectedPolicy?.policy_id === policy.policy_id) resetForm();
      await refresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const fieldStyle = { padding: "8px 10px", border: "1px solid var(--line)", borderRadius: "7px", background: "var(--surface)", fontSize: "13px", width: "100%" };
  const labelStyle = { display: "grid", gap: "5px", color: "var(--text-soft)", fontSize: "12px" };

  return (
    <section>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "16px", marginBottom: "16px" }}>
        <div>
          <h2 style={{ margin: "0 0 6px", fontSize: "18px" }}>权限管理</h2>
          <p style={{ margin: 0, color: "var(--text-soft)", fontSize: "13px" }}>按 OA 部门授权测试造数能力和任务；系统级黑名单始终优先。</p>
        </div>
        <button className="button button-secondary" onClick={() => void refresh()} disabled={loading || busy}>刷新</button>
      </div>
      {error ? <p style={{ color: "#721c24", background: "#f8d7da", padding: "8px 12px", borderRadius: "7px" }}>{error}</p> : null}
      {!loading && !departments.length ? <p style={{ color: "#8f2f2f", background: "#f8e8e5", padding: "8px 12px", borderRadius: "7px" }}>OA 部门目录不可用，暂时不能创建策略；已有策略仍按安全默认值拒绝受限访问。</p> : null}

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1.2fr) minmax(360px, .8fr)", gap: "18px", alignItems: "start" }}>
        <div>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "8px" }}>
            <h3 style={{ margin: 0, fontSize: "15px" }}>策略列表</h3>
            <button className="button button-primary" style={{ fontSize: "12px", padding: "5px 10px" }} onClick={beginCreatePolicy}>新增策略</button>
          </div>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "13px" }}>
              <thead><tr style={{ borderBottom: "2px solid var(--line)", textAlign: "left" }}>
                <th style={{ padding: "9px" }}>策略</th><th style={{ padding: "9px" }}>部门范围</th><th style={{ padding: "9px" }}>任务</th><th style={{ padding: "9px" }}>状态</th><th style={{ padding: "9px" }}>操作</th>
              </tr></thead>
              <tbody>
                {policies.map((policy) => (
                  <tr key={policy.policy_id} style={{ borderBottom: "1px solid var(--line-soft)", cursor: "pointer" }} onClick={() => viewPolicy(policy)}>
                    <td style={{ padding: "9px", minWidth: "150px" }}><strong>{policy.policy_name}</strong><div style={{ color: "var(--text-faint)", fontSize: "11px" }}>{policy.organization_key} · {policy.capability_key} · 命中 {policy.matched_user_count || 0} 人</div></td>
                    <td style={{ padding: "9px", maxWidth: "240px" }}>{(policy.departments || []).map((item) => `${item.department_path_snapshot}${item.include_children ? "（含子部门）" : ""}`).join("、")}</td>
                    <td style={{ padding: "9px" }}>UAT 环境控制</td>
                    <td style={{ padding: "9px", whiteSpace: "nowrap" }}><button type="button" className="button button-secondary" style={{ fontSize: "12px", padding: "4px 8px", color: policy.status === "enabled" ? "#3f684d" : "var(--text-faint)" }} title={`点击${policy.status === "enabled" ? "停用" : "启用"}`} onClick={(event) => { event.stopPropagation(); void togglePolicy(policy); }} disabled={busy}>{policy.status === "enabled" ? "启用" : "停用"}</button></td>
                    <td style={{ padding: "9px", whiteSpace: "nowrap", display: "flex", gap: "6px" }}><button type="button" className="button button-secondary" style={{ fontSize: "12px", padding: "4px 8px" }} onClick={(event) => { event.stopPropagation(); editPolicy(policy); }}>编辑</button><button type="button" className="button button-secondary" style={{ fontSize: "12px", padding: "4px 8px", color: "#8f2f2f" }} onClick={(event) => { event.stopPropagation(); void removePolicy(policy); }} disabled={busy}>删除</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!loading && !policies.length ? <p style={{ color: "var(--text-faint)", textAlign: "center", padding: "24px 0" }}>暂无策略。当前只会拒绝测试造数，其他 ai-prd 功能不受影响；请先按 OA 部门配置基线策略。</p> : null}
        </div>

        {panelMode === "empty" ? (
          <div style={{ border: "1px dashed var(--line)", borderRadius: "10px", padding: "28px 18px", background: "var(--surface-strong)", color: "var(--text-faint)", textAlign: "center", minHeight: "220px", display: "grid", placeItems: "center" }}>
            <div><div style={{ color: "var(--text-soft)", fontSize: "15px", fontWeight: 600 }}>选择一个策略查看内容</div><div style={{ marginTop: "7px", fontSize: "13px", lineHeight: 1.5 }}>也可以点击“新增策略”进入编辑模式。</div></div>
          </div>
        ) : panelMode === "view" && selectedPolicy ? (
          <PermissionPolicyReadOnly policy={selectedPolicy} audit={audit} onEdit={() => editPolicy(selectedPolicy)} onDelete={() => void removePolicy(selectedPolicy)} />
        ) : (
        <form onSubmit={submit} style={{ border: editingPolicyId ? "2px solid #c96442" : "1px solid var(--line)", borderRadius: "10px", padding: "14px", background: editingPolicyId ? "#fff8f3" : "var(--surface-strong)", boxShadow: editingPolicyId ? "0 0 0 3px rgba(201, 100, 66, .12)" : "none", display: "grid", gap: "10px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "10px" }}><div><span style={{ display: "inline-block", color: editingPolicyId ? "#93492f" : "var(--text-soft)", background: editingPolicyId ? "#f8eee8" : "var(--surface-muted)", borderRadius: "999px", padding: "3px 8px", fontSize: "11px", fontWeight: 600, marginBottom: "5px" }}>{editingPolicyId ? "编辑模式" : "新建模式"}</span><h3 style={{ margin: 0, fontSize: "15px" }}>{editingPolicyId ? `编辑策略：${form.policy_name || "未命名"}` : "新增策略"}</h3></div>{editingPolicyId ? <div style={{ display: "flex", gap: "6px" }}><button type="button" className="button button-secondary" style={{ fontSize: "12px", padding: "4px 8px", color: "#8f2f2f" }} onClick={() => void removePolicy({ policy_id: editingPolicyId, policy_name: form.policy_name })} disabled={busy}>删除策略</button><button type="button" className="button button-secondary" style={{ fontSize: "12px", padding: "4px 8px" }} onClick={resetForm}>取消编辑</button></div> : null}</div>
          <label style={labelStyle}>策略名称（可选）<input maxLength="120" placeholder="留空由 light 模型自动生成" style={fieldStyle} value={form.policy_name} onChange={(e) => setForm({ ...form, policy_name: e.target.value })} /></label>
          <label style={labelStyle}>组织<select style={fieldStyle} value={form.organization_key} onChange={(e) => setForm({ ...form, organization_key: e.target.value })}>{organizations.map((item) => <option key={item.organization_key} value={item.organization_key}>{item.display_name || item.organization_key}</option>)}</select></label>
          <label style={labelStyle}>OA 部门（可多选）<select required multiple size="10" style={{ ...fieldStyle, minHeight: "220px" }} value={form.department_keys} onChange={(e) => setForm({ ...form, department_keys: Array.from(e.target.selectedOptions, (option) => option.value) })}>{departments.map((item) => <option key={item.key} value={item.key}>{item.path}</option>)}</select></label>
          <label style={{ display: "flex", gap: "7px", alignItems: "center", color: "var(--text-soft)", fontSize: "13px" }}><input type="checkbox" checked={form.include_children} onChange={(e) => setForm({ ...form, include_children: e.target.checked })} />包含子部门</label>
          <div style={{ border: "1px solid var(--line-soft)", borderRadius: "8px", padding: "10px 11px", background: "var(--surface)", color: "var(--text-soft)", fontSize: "13px" }}><div style={{ color: "var(--text)", fontWeight: 600 }}>授权任务：UAT 环境控制</div><div style={{ marginTop: "4px", lineHeight: 1.5 }}>授权后可使用系统当前开放的 UAT 环境和造数任务；系统级白名单和黑名单仍然有效。</div></div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}><label style={labelStyle}>开始时间<input type="datetime-local" style={fieldStyle} value={form.starts_at} onChange={(e) => setForm({ ...form, starts_at: e.target.value })} /></label><label style={labelStyle}>结束时间<input type="datetime-local" style={fieldStyle} value={form.expires_at} onChange={(e) => setForm({ ...form, expires_at: e.target.value })} /></label></div>
          <label style={labelStyle}>状态<select style={fieldStyle} value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}><option value="enabled">启用</option><option value="disabled">停用</option></select></label>
          <label style={labelStyle}>说明<textarea rows="2" maxLength="1000" style={{ ...fieldStyle, resize: "vertical" }} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></label>
          <button className="button button-primary" type="submit" disabled={busy || !departments.length}>{busy ? "保存中…" : "保存策略"}</button>
          {audit.length ? <div style={{ borderTop: "1px solid var(--line-soft)", paddingTop: "9px", color: "var(--text-faint)", fontSize: "11px" }}><strong>审计记录</strong>{audit.map((item) => <div key={item.audit_id} style={{ marginTop: "5px" }}>{item.action} · {item.actor_user_id} · {formatDateTime(item.created_at)}</div>)}</div> : null}
        </form>
        )}
      </div>

      <form onSubmit={checkUser} style={{ marginTop: "18px", borderTop: "1px solid var(--line)", paddingTop: "16px" }}>
        <h3 style={{ margin: "0 0 10px", fontSize: "15px" }}>检查用户权限</h3>
        <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr auto", gap: "8px", alignItems: "end" }}>
          <label style={labelStyle}>用户<select required style={fieldStyle} value={checkUserId} onChange={(e) => setCheckUserId(e.target.value)}>{users.map((item) => <option key={item.user_id} value={item.user_id}>{userLabel(item)} · {item.email}</option>)}</select></label>
          <label style={labelStyle}>组织<select required style={fieldStyle} value={checkOrganizationKey} onChange={(e) => setCheckOrganizationKey(e.target.value)}>{organizations.map((item) => <option key={item.organization_key} value={item.organization_key}>{item.display_name || item.organization_key}</option>)}</select></label>
          <button className="button button-secondary" type="submit" disabled={busy || !checkUserId}>检查</button>
        </div>
        {checkResult ? <div style={{ marginTop: "12px", border: "1px solid var(--line)", borderRadius: "8px", padding: "11px", background: "var(--surface-strong)", fontSize: "13px" }}><div><strong>{checkResult.user?.name || checkResult.user?.email}</strong> · OA 部门：{checkResult.department_snapshot?.department_paths?.join("、") || "未获取"}</div><div style={{ marginTop: "7px", color: checkResult.decision?.allowed ? "#3f684d" : "#8f2f2f", fontWeight: 600 }}>{checkResult.decision?.allowed ? "允许" : "拒绝"}：{checkResult.decision?.reason}</div>{checkResult.decision?.matched_policy_name ? <div style={{ marginTop: "4px", color: "var(--text-soft)" }}>命中策略：{checkResult.decision.matched_policy_name}</div> : null}</div> : null}
      </form>
    </section>
  );
}

export default function AdminApp() {
  const [adminUser, setAdminUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [authError, setAuthError] = useState("");
  const [adminView, setAdminView] = useState(() => {
    const params = new URLSearchParams(window.location.search);
    const ops = params.get("ops");
    if (ops === "chat-logs") return "chatLogs";
    if (ops === "image-usage") return "imageUsage";
    if (ops === "business-docs") return "businessDocs";
    if (ops === "permissions") return "permissions";
    return "users";
  });
  const [users, setUsers] = useState([]);
  const [statusFilter, setStatusFilter] = useState("active");
  const [usersLoading, setUsersLoading] = useState(false);
  const [actionError, setActionError] = useState("");
  const [agentRequests, setAgentRequests] = useState([]);
  const [agentRequestFilter, setAgentRequestFilter] = useState("pending");
  const [agentRequestsLoading, setAgentRequestsLoading] = useState(false);
  const [agentRequestsError, setAgentRequestsError] = useState("");
  const [chatSessions, setChatSessions] = useState([]);
  const [chatQuery, setChatQuery] = useState("");
  const [chatUsers, setChatUsers] = useState([]);
  const [chatUsersLoading, setChatUsersLoading] = useState(false);
  const [chatUserPickerOpen, setChatUserPickerOpen] = useState(false);
  const [chatUserFilterInput, setChatUserFilterInput] = useState("");
  const [selectedChatUserId, setSelectedChatUserId] = useState("");
  const [chatPage, setChatPage] = useState(0);
  const [chatHasNextPage, setChatHasNextPage] = useState(false);
  const [selectedChatSessionId, setSelectedChatSessionId] = useState("");
  const [selectedChatDetail, setSelectedChatDetail] = useState(null);
  const [chatLoading, setChatLoading] = useState(false);
  const [chatDetailLoading, setChatDetailLoading] = useState(false);
  const [chatError, setChatError] = useState("");
  const [chatRefreshToken, setChatRefreshToken] = useState(0);
  const [organizations, setOrganizations] = useState([]);
  const [selectedOrganizationUser, setSelectedOrganizationUser] = useState(null);
  const [organizationMemberships, setOrganizationMemberships] = useState([]);
  const [organizationAccessRequests, setOrganizationAccessRequests] = useState([]);
  const [organizationPanelLoading, setOrganizationPanelLoading] = useState(false);
  const [organizationPanelError, setOrganizationPanelError] = useState("");
  const [imageUsage, setImageUsage] = useState([]);
  const [imageUsageLoading, setImageUsageLoading] = useState(false);
  const [imageUsageError, setImageUsageError] = useState("");
  const [usageQuery, setUsageQuery] = useState("");
  const [usageSort, setUsageSort] = useState("total");
  const [businessDocRuns, setBusinessDocRuns] = useState([]);
  const [businessDocRunDetail, setBusinessDocRunDetail] = useState(null);
  const [businessDocRunsLoading, setBusinessDocRunsLoading] = useState(false);
  const [businessDocRunsBusy, setBusinessDocRunsBusy] = useState(false);
  const [businessDocRunsError, setBusinessDocRunsError] = useState("");
  const [businessDocUpdateDetail, setBusinessDocUpdateDetail] = useState(null);
  const [businessDocUpdateLoading, setBusinessDocUpdateLoading] = useState(false);
  const [businessDocUpdateBusy, setBusinessDocUpdateBusy] = useState(false);
  const [businessDocUpdateAction, setBusinessDocUpdateAction] = useState("");
  const [businessDocBatchProgress, setBusinessDocBatchProgress] = useState(null);
  const [businessDocUpdateError, setBusinessDocUpdateError] = useState("");

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const err = params.get("auth_error");
    if (err) {
      setAuthError(err);
      params.delete("auth_error");
      const qs = params.toString();
      window.history.replaceState(null, "", `${window.location.pathname}${qs ? `?${qs}` : ""}`);
    }
  }, []);

  useEffect(() => {
    getAdminMe()
      .then(setAdminUser)
      .catch((e) => setAuthError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!adminUser || adminView !== "users") return;
    setUsersLoading(true);
    setActionError("");
    listUsers(statusFilter || null)
      .then(setUsers)
      .catch((e) => setActionError(e.message))
      .finally(() => setUsersLoading(false));
  }, [adminUser, adminView, statusFilter]);

  useEffect(() => {
    if (!adminUser || adminView !== "agentReview") return;
    setAgentRequestsLoading(true);
    setAgentRequestsError("");
    listAgentAccessRequests(agentRequestFilter || null)
      .then(setAgentRequests)
      .catch((e) => setAgentRequestsError(e.message))
      .finally(() => setAgentRequestsLoading(false));
  }, [adminUser, adminView, agentRequestFilter]);

  useEffect(() => {
    if (!adminUser || adminView !== "imageUsage") return;
    setImageUsageLoading(true);
    setImageUsageError("");
    listImageUsage()
      .then(setImageUsage)
      .catch((e) => setImageUsageError(e.message))
      .finally(() => setImageUsageLoading(false));
  }, [adminUser, adminView]);

  useEffect(() => {
    if (!adminUser || adminView !== "businessDocs") return;
    let disposed = false;
    async function refresh(showLoading = false) {
      if (showLoading) setBusinessDocRunsLoading(true);
      try {
        const runs = await listBusinessDocRuns();
        if (disposed) return;
        setBusinessDocRuns(runs);
        const selectedId = businessDocRunDetail?.run_id || runs[0]?.run_id;
        setBusinessDocRunDetail(selectedId ? await getBusinessDocRun(selectedId) : null);
        setBusinessDocRunsError("");
      } catch (e) {
        if (!disposed) setBusinessDocRunsError(e.message);
      } finally {
        if (!disposed && showLoading) setBusinessDocRunsLoading(false);
      }
    }
    void refresh(true);
    const intervalId = window.setInterval(() => void refresh(false), 5000);
    return () => { disposed = true; window.clearInterval(intervalId); };
  }, [adminUser, adminView, businessDocRunDetail?.run_id]);

  useEffect(() => {
    if (!adminUser || adminView !== "users") return;
    let disposed = false;
    Promise.all([
      listOrganizations(),
      listOrganizationAccessRequests("pending"),
    ])
      .then(([orgs, requests]) => {
        if (disposed) return;
        setOrganizations(Array.isArray(orgs) ? orgs : []);
        setOrganizationAccessRequests(Array.isArray(requests) ? requests : []);
      })
      .catch((error) => {
        if (!disposed) {
          setActionError(error.message);
        }
      });
    return () => {
      disposed = true;
    };
  }, [adminUser, adminView]);

  useEffect(() => {
    function handleHiddenOpsShortcut(event) {
      if (!adminUser) return;
      if ((event.metaKey || event.ctrlKey) && event.altKey && event.shiftKey && event.code === "KeyL") {
        event.preventDefault();
        setAdminView((current) => {
          const next = current === "chatLogs" ? "users" : "chatLogs";
          const params = new URLSearchParams(window.location.search);
          if (next === "chatLogs") {
            params.set("ops", "chat-logs");
          } else {
            params.delete("ops");
          }
          const qs = params.toString();
          window.history.replaceState(null, "", `${window.location.pathname}${qs ? `?${qs}` : ""}`);
          return next;
        });
      }
    }

    window.addEventListener("keydown", handleHiddenOpsShortcut);
    return () => window.removeEventListener("keydown", handleHiddenOpsShortcut);
  }, [adminUser]);

  useEffect(() => {
    if (!adminUser || adminView !== "chatLogs") return;
    let disposed = false;
    let requestInFlight = false;

    async function refreshChatSessions(showLoading) {
      if (requestInFlight) return;
      requestInFlight = true;
      if (showLoading) {
        setChatLoading(true);
        setChatError("");
      }

      try {
        const fetchedSessions = await listChatSessions({
          query: chatQuery,
          userId: selectedChatUserId,
          limit: chatSessionPageSize + 1,
          offset: chatPage * chatSessionPageSize,
        });
        if (disposed) return;
        const visibleSessions = fetchedSessions.slice(0, chatSessionPageSize);
        setChatSessions(visibleSessions);
        setChatHasNextPage(fetchedSessions.length > chatSessionPageSize);
        setSelectedChatSessionId((current) => {
          if (current && visibleSessions.some((session) => session.session_id === current)) {
            return current;
          }
          return visibleSessions[0]?.session_id || "";
        });
      } catch (error) {
        if (!disposed && showLoading) {
          setChatSessions([]);
          setChatHasNextPage(false);
          setSelectedChatSessionId("");
          setChatError(error.message);
        }
      } finally {
        requestInFlight = false;
        if (!disposed && showLoading) {
          setChatLoading(false);
        }
      }
    }

    void refreshChatSessions(true);
    const intervalId = window.setInterval(() => {
      void refreshChatSessions(false);
    }, chatSessionPollIntervalMs);

    return () => {
      disposed = true;
      window.clearInterval(intervalId);
    };
  }, [adminUser, adminView, chatQuery, selectedChatUserId, chatPage, chatRefreshToken]);

  useEffect(() => {
    if (!adminUser || adminView !== "chatLogs" || !selectedChatSessionId) {
      setSelectedChatDetail(null);
      return;
    }

    let disposed = false;
    setChatDetailLoading(true);
    setChatError("");
    getChatSession(selectedChatSessionId)
      .then((detail) => {
        if (!disposed) {
          setSelectedChatDetail(detail);
        }
      })
      .catch((e) => {
        if (!disposed) {
          setSelectedChatDetail(null);
          setChatError(e.message);
        }
      })
      .finally(() => {
        if (!disposed) {
          setChatDetailLoading(false);
        }
      });

    return () => {
      disposed = true;
    };
  }, [adminUser, adminView, selectedChatSessionId]);

  async function handleSetAgentAccess(userId, agentAccess) {
    setActionError("");
    try {
      const updated = await setUserAgentAccess(userId, agentAccess);
      setUsers((prev) => prev.map((user) => (user.user_id === updated.user_id ? updated : user)));
    } catch (e) {
      setActionError(e.message);
    }
  }

  function updateUserAfterStatusChange(updated) {
    setUsers((prev) => {
      if (statusFilter && updated.status !== statusFilter) {
        return prev.filter((user) => user.user_id !== updated.user_id);
      }
      return prev.map((user) => (user.user_id === updated.user_id ? updated : user));
    });
  }

  async function handleReviewAgentRequest(requestId, action) {
    setActionError("");
    try {
      await reviewAgentAccessRequest(requestId, action);
      setAgentRequests((prev) => prev.filter((item) => item.request_id !== requestId));
    } catch (e) {
      setActionError(e.message);
    }
  }

  async function handleBlock(userId) {
    setActionError("");
    try {
      const updated = await blockUser(userId);
      updateUserAfterStatusChange(updated);
    } catch (e) {
      setActionError(e.message);
    }
  }

  async function handleSetRole(userId, role) {
    setActionError("");
    try {
      const updated = await setUserRole(userId, role);
      setUsers((prev) => prev.map((u) => (u.user_id === userId ? updated : u)));
    } catch (e) {
      setActionError(e.message);
    }
  }

  async function refreshOrganizationPanel(user = selectedOrganizationUser) {
    if (!user) return;
    setOrganizationPanelLoading(true);
    setOrganizationPanelError("");
    try {
      const [memberships, requests] = await Promise.all([
        listUserOrganizationMemberships(user.user_id),
        listOrganizationAccessRequests("pending"),
      ]);
      setOrganizationMemberships(Array.isArray(memberships) ? memberships : []);
      setOrganizationAccessRequests(Array.isArray(requests) ? requests.filter((request) => request.requester_user_id === user.user_id) : []);
    } catch (e) {
      setOrganizationPanelError(e.message);
    } finally {
      setOrganizationPanelLoading(false);
    }
  }

  async function handleManageOrganizations(user) {
    if (selectedOrganizationUser?.user_id === user.user_id) {
      setSelectedOrganizationUser(null);
      return;
    }
    setSelectedOrganizationUser(user);
    await refreshOrganizationPanel(user);
  }

  async function handleAssignOrganization(organizationKey, isDefault) {
    if (!selectedOrganizationUser) return;
    setOrganizationPanelError("");
    try {
      await assignUserOrganization(selectedOrganizationUser.user_id, {
        organization_key: organizationKey,
        organization_role: "member",
        is_default: isDefault,
        status: "active",
      });
      await refreshOrganizationPanel(selectedOrganizationUser);
      setUsers(await listUsers(statusFilter || null));
    } catch (e) {
      setOrganizationPanelError(e.message);
    }
  }

  async function handleUpdateMembership(membership, payload) {
    if (!selectedOrganizationUser) return;
    setOrganizationPanelError("");
    try {
      await updateUserOrganizationMembership(selectedOrganizationUser.user_id, membership.membership_id, payload);
      await refreshOrganizationPanel(selectedOrganizationUser);
      setUsers(await listUsers(statusFilter || null));
    } catch (e) {
      setOrganizationPanelError(e.message);
    }
  }

  async function handleReviewOrganizationRequest(requestId, action) {
    setOrganizationPanelError("");
    try {
      await reviewOrganizationAccessRequest(requestId, action);
      await refreshOrganizationPanel(selectedOrganizationUser);
    } catch (e) {
      setOrganizationPanelError(e.message);
    }
  }

  async function handleLogout() {
    await adminLogout().catch(() => {});
    setAdminUser(null);
  }

  async function handleStartBusinessDocRun() {
    setBusinessDocRunsBusy(true);
    setBusinessDocRunsError("");
    setBusinessDocUpdateDetail(null);
    setBusinessDocUpdateError("");
    try {
      const run = await startBusinessDocFullRun();
      setBusinessDocRunDetail(await getBusinessDocRun(run.run_id));
      setBusinessDocRuns(await listBusinessDocRuns());
    } catch (e) {
      setBusinessDocRunsError(e.message);
    } finally {
      setBusinessDocRunsBusy(false);
    }
  }

  async function handleRetryBusinessDocRun(runId) {
    setBusinessDocRunsBusy(true);
    setBusinessDocRunsError("");
    setBusinessDocUpdateDetail(null);
    setBusinessDocUpdateError("");
    try {
      await retryBusinessDocRun(runId);
      setBusinessDocRunDetail(await getBusinessDocRun(runId));
      setBusinessDocRuns(await listBusinessDocRuns());
    } catch (e) {
      setBusinessDocRunsError(e.message);
    } finally {
      setBusinessDocRunsBusy(false);
    }
  }

  async function handleOpenBusinessDocUpdate(updateId) {
    setBusinessDocUpdateLoading(true);
    setBusinessDocUpdateError("");
    try {
      setBusinessDocUpdateDetail(await getAdminBusinessDocUpdate(updateId));
    } catch (e) {
      setBusinessDocUpdateError(e.message);
    } finally {
      setBusinessDocUpdateLoading(false);
    }
  }

  async function handleBusinessDocUpdateAction(action) {
    if (!businessDocUpdateDetail?.update_id) return;
    setBusinessDocUpdateBusy(true);
    setBusinessDocUpdateAction(action);
    setBusinessDocUpdateError("");
    try {
      const actionHandler = action === "apply" ? applyAdminBusinessDocUpdate : ignoreAdminBusinessDocUpdate;
      const nextDetail = await actionHandler(businessDocUpdateDetail.update_id);
      setBusinessDocUpdateDetail(nextDetail);
      if (businessDocRunDetail?.run_id) {
        setBusinessDocRunDetail(await getBusinessDocRun(businessDocRunDetail.run_id));
      }
    } catch (e) {
      setBusinessDocUpdateError(e.message);
    } finally {
      setBusinessDocUpdateBusy(false);
      setBusinessDocUpdateAction("");
    }
  }

  async function handleApplyHighConfidenceBusinessDocUpdates(items) {
    if (!businessDocRunDetail?.run_id || !items.length) return;
    const runId = businessDocRunDetail.run_id;
    const failures = [];
    setBusinessDocBatchProgress({ completed: 0, total: items.length });
    setBusinessDocUpdateError("");
    try {
      for (const [index, item] of items.entries()) {
        try {
          await applyAdminBusinessDocUpdate(item.update_id);
        } catch (e) {
          failures.push(`${item.source_path || item.update_id}：${e.message}`);
        }
        setBusinessDocBatchProgress({ completed: index + 1, total: items.length });
      }
      setBusinessDocRunDetail(await getBusinessDocRun(runId));
      setBusinessDocRuns(await listBusinessDocRuns());
      if (failures.length) {
        setBusinessDocUpdateError(`有 ${failures.length} 个提案应用失败：${failures.join("；")}`);
      }
    } catch (e) {
      setBusinessDocUpdateError(e.message);
    } finally {
      setBusinessDocBatchProgress(null);
    }
  }

  function showUserManagement() {
    const params = new URLSearchParams(window.location.search);
    params.delete("ops");
    const qs = params.toString();
    window.history.replaceState(null, "", `${window.location.pathname}${qs ? `?${qs}` : ""}`);
    setAdminView("users");
  }

  function handleChatQueryChange(value) {
    setChatQuery(value);
    setChatPage(0);
  }

  function handleSelectChatUser(userId) {
    const selectedUser = chatUsers.find((user) => user.user_id === userId);
    setSelectedChatUserId(userId);
    setChatUserFilterInput(selectedUser ? userLabel(selectedUser) : "");
    setChatPage(0);
    setChatUserPickerOpen(false);
  }

  async function loadChatUsersIfNeeded() {
    if (chatUsers.length || chatUsersLoading) {
      return;
    }

    setChatUsersLoading(true);
    setChatError("");
    try {
      const allUsers = await listUsers(null);
      setChatUsers(sortUsersAlphabetically(allUsers));
    } catch (e) {
      setChatError(e.message);
    } finally {
      setChatUsersLoading(false);
    }
  }

  async function handleOpenChatUserPicker() {
    setChatUserPickerOpen(true);
    await loadChatUsersIfNeeded();
  }

  async function handleToggleChatUserPicker() {
    const nextOpen = !chatUserPickerOpen;
    setChatUserPickerOpen(nextOpen);
    if (nextOpen) {
      await loadChatUsersIfNeeded();
    }
  }

  function handleChatUserFilterInputChange(value) {
    setChatUserFilterInput(value);
    setChatUserPickerOpen(true);
    if (selectedChatUserId) {
      setSelectedChatUserId("");
      setChatPage(0);
    }
    void loadChatUsersIfNeeded();
  }

  const shell = {
    minHeight: "100vh",
    background: "var(--bg)",
    fontFamily: "var(--font-sans)",
  };

  const header = {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "0 24px",
    height: "56px",
    background: "var(--surface-strong)",
    borderBottom: "1px solid var(--line)",
  };

  if (loading) {
    return (
      <div style={{ ...shell, display: "flex", alignItems: "center", justifyContent: "center" }}>
        <p style={{ color: "var(--text-faint)" }}>正在验证管理员身份…</p>
      </div>
    );
  }

  if (!adminUser) {
    return (
      <div style={{ ...shell, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: "24px" }}>
        <h1 style={{ margin: 0, fontSize: "24px" }}>ai-prd 管理后台</h1>
        <p style={{ margin: 0, color: "var(--text-soft)" }}>请使用管理员 Google 账号登录</p>
        {authError && <p style={{ color: "#721c24", margin: 0 }}>{authError}</p>}
        <button
          className="button button-primary"
          onClick={() => { window.location.assign("/api/admin/auth/google/login"); }}
        >
          使用 Google 登录（管理员）
        </button>
      </div>
    );
  }

  const tabs = [
    { label: "正常", value: "active" },
    { label: "已封禁", value: "blocked" },
    { label: "全部", value: "" },
  ];

  const contentMaxWidth =
    adminView === "chatLogs" || adminView === "businessDocs"
      ? "1240px"
      : adminView === "imageUsage"
        ? "1080px"
        : "1440px";

  return (
    <div style={shell}>
      <div style={header}>
        <span style={{ fontWeight: 600, fontSize: "16px" }}>ai-prd 管理后台</span>
        <div style={{ display: "flex", alignItems: "center", gap: "16px" }}>
          <span style={{ fontSize: "14px", color: "var(--text-soft)" }}>{adminUser.name || adminUser.email}</span>
          <button className="button button-secondary" style={{ fontSize: "13px" }} onClick={handleLogout}>
            退出
          </button>
        </div>
      </div>

      <div style={{ maxWidth: contentMaxWidth, margin: "0 auto", padding: "16px 24px 0", display: "flex", gap: "8px" }}>
        {[
          { value: "users", label: "用户管理" },
          { value: "agentReview", label: "Agent 审核" },
          { value: "permissions", label: "高级权限管理" },
          { value: "imageUsage", label: "生图用量" },
          { value: "businessDocs", label: "业务文档更新" },
        ].map((item) => (
          <button
            key={item.value}
            className={`button ${adminView === item.value ? "button-primary" : "button-secondary"}`}
            style={{ fontSize: "13px" }}
            onClick={() => setAdminView(item.value)}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div
        style={{
          maxWidth: contentMaxWidth,
          margin: "0 auto",
          padding: "24px",
        }}
      >
        {adminView === "permissions" ? (
          <PermissionPoliciesPanel />
        ) : adminView === "chatLogs" ? (
          <ChatLogsPanel
            sessions={chatSessions}
            selectedSessionId={selectedChatSessionId}
            detail={selectedChatDetail}
            query={chatQuery}
            users={chatUsers}
            selectedUserId={selectedChatUserId}
            userFilterInput={chatUserFilterInput}
            userPickerOpen={chatUserPickerOpen}
            usersLoading={chatUsersLoading}
            page={chatPage}
            pageSize={chatSessionPageSize}
            hasNextPage={chatHasNextPage}
            loading={chatLoading}
            detailLoading={chatDetailLoading}
            error={chatError}
            onQueryChange={handleChatQueryChange}
            onUserFilterInputChange={handleChatUserFilterInputChange}
            onOpenUserPicker={handleOpenChatUserPicker}
            onToggleUserPicker={handleToggleChatUserPicker}
            onSelectUser={handleSelectChatUser}
            onSelectSession={setSelectedChatSessionId}
            onPreviousPage={() => setChatPage((current) => Math.max(0, current - 1))}
            onNextPage={() => setChatPage((current) => current + 1)}
            onRefresh={() => setChatRefreshToken((current) => current + 1)}
            onBack={showUserManagement}
          />
        ) : adminView === "businessDocs" ? (
          <BusinessDocRunsPanel
            runs={businessDocRuns}
            detail={businessDocRunDetail}
            updateDetail={businessDocUpdateDetail}
            loading={businessDocRunsLoading}
            busy={businessDocRunsBusy}
            updateLoading={businessDocUpdateLoading}
            updateBusy={businessDocUpdateBusy}
            updateAction={businessDocUpdateAction}
            batchProgress={businessDocBatchProgress}
            error={businessDocRunsError}
            updateError={businessDocUpdateError}
            onStart={handleStartBusinessDocRun}
            onSelect={(runId) => {
              setBusinessDocUpdateDetail(null);
              setBusinessDocUpdateError("");
              setBusinessDocRunsLoading(true);
              getBusinessDocRun(runId)
                .then(setBusinessDocRunDetail)
                .catch((e) => setBusinessDocRunsError(e.message))
                .finally(() => setBusinessDocRunsLoading(false));
            }}
            onRetry={handleRetryBusinessDocRun}
            onOpenUpdate={handleOpenBusinessDocUpdate}
            onCloseUpdate={() => {
              setBusinessDocUpdateDetail(null);
              setBusinessDocUpdateError("");
            }}
            onApplyUpdate={() => handleBusinessDocUpdateAction("apply")}
            onApplyHighConfidence={handleApplyHighConfidenceBusinessDocUpdates}
            onIgnoreUpdate={() => handleBusinessDocUpdateAction("ignore")}
            onRefresh={() => {
              setBusinessDocRunsLoading(true);
              listBusinessDocRuns()
                .then(setBusinessDocRuns)
                .catch((e) => setBusinessDocRunsError(e.message))
                .finally(() => setBusinessDocRunsLoading(false));
            }}
          />
        ) : adminView === "imageUsage" ? (
          <ImageUsagePanel
            rows={imageUsage}
            loading={imageUsageLoading}
            error={imageUsageError}
            query={usageQuery}
            onQueryChange={setUsageQuery}
            sort={usageSort}
            onSort={setUsageSort}
            onRefresh={() => {
              setImageUsageLoading(true);
              setImageUsageError("");
              listImageUsage()
                .then(setImageUsage)
                .catch((e) => setImageUsageError(e.message))
                .finally(() => setImageUsageLoading(false));
            }}
          />
        ) : adminView === "agentReview" ? (
          <AgentReviewPanel
            requests={agentRequests}
            filter={agentRequestFilter}
            loading={agentRequestsLoading}
            error={agentRequestsError || actionError}
            onFilterChange={setAgentRequestFilter}
            onReview={handleReviewAgentRequest}
          />
        ) : (
          <>
            <h2 style={{ margin: "0 0 20px", fontSize: "18px" }}>用户管理</h2>

            <div style={{ display: "flex", gap: "8px", marginBottom: "16px" }}>
              {tabs.map((t) => (
                <button
                  key={t.value}
                  className={`button ${statusFilter === t.value ? "button-primary" : "button-secondary"}`}
                  style={{ fontSize: "13px" }}
                  onClick={() => setStatusFilter(t.value)}
                >
                  {t.label}
                </button>
              ))}
            </div>

            {actionError && (
              <p style={{ color: "#721c24", background: "#f8d7da", padding: "8px 12px", borderRadius: "6px", marginBottom: "12px" }}>
                {actionError}
              </p>
            )}

            {usersLoading ? (
              <p style={{ color: "var(--text-faint)" }}>加载中…</p>
            ) : (
              <UserTable
                users={users}
                currentUserId={adminUser.user_id}
                onBlock={handleBlock}
                onSetAgentAccess={handleSetAgentAccess}
                onSetRole={handleSetRole}
                onManageOrganizations={(user) => void handleManageOrganizations(user)}
                selectedOrganizationUser={selectedOrganizationUser}
                organizations={organizations}
                memberships={organizationMemberships}
                accessRequests={organizationAccessRequests}
                organizationPanelLoading={organizationPanelLoading}
                organizationPanelError={organizationPanelError}
                onCloseOrganizationPanel={() => setSelectedOrganizationUser(null)}
                onAssignOrganization={handleAssignOrganization}
                onUpdateMembership={handleUpdateMembership}
                onApproveOrganizationRequest={(requestId) => handleReviewOrganizationRequest(requestId, "approve")}
                onRejectOrganizationRequest={(requestId) => handleReviewOrganizationRequest(requestId, "reject")}
              />
            )}
          </>
        )}
      </div>
    </div>
  );
}
