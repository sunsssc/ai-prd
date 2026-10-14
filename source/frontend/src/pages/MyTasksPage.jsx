import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import Badge from "../components/common/Badge";
import DocumentDetailPanel from "../components/common/DocumentDetailPanel";
import MarkdownRenderer from "../components/common/MarkdownRenderer";
import ReferenceIcon from "../components/common/ReferenceIcon";
import {
  buildKnowledgeAssetUrl,
  createMyTaskAssistantSession,
  createManualCodeReview,
  getCodeReview,
  getCodeReviewJob,
  getKnowledgeFile,
  getRequirementClickUpEditAccess,
  listCodeReviewJobs,
  listMyTasks,
  updateMyTaskEnvironment
} from "../services/workspaceApi";
import {
  buildTaskRequirementReference,
  prStatusMeta,
  shortRepoName
} from "../utils/requirementPrLinks";

const ClickUpContentEditor = lazy(() => import("../components/common/ClickUpContentEditor"));

function ReadOnlyDocumentIcon() {
  return (
    <svg
      className="clickup-content-readonly-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M7 3.75h7l3.25 3.25v13.25H7z" />
      <path d="M14 3.75V7h3.25" />
      <path d="M9.5 11h5" />
      <path d="M9.5 14.5h5" />
    </svg>
  );
}

const STATUS_TONES = {
  实现中: "olive",
  测试中: "sand",
  待测试: "sand",
  验收中: "sand",
  待验收: "sand",
  待上线: "olive",
  已上线: "neutral",
  Closed: "neutral",
  已拒绝: "signal",
  已挂起: "signal"
};

const REVIEW_STATUS = {
  pending: { label: "审核排队中", tone: "sand" },
  running: { label: "审核中", tone: "sand" },
  completed: { label: "已审核", tone: "olive" },
  failed: { label: "审核失败", tone: "signal" }
};

const ROLE_ORDER = ["负责人", "前端", "后端", "产品", "测试", "抄送人"];
const PAGE_SIZE = 50;

function statusTone(status) {
  return STATUS_TONES[status] || "neutral";
}

function currentEnvironment(task) {
  return task.manual_environment || task.detected_environments?.[0] || "";
}

function sameSelection(left, right) {
  if (left.length !== right.length) return false;
  const rightValues = new Set(right);
  return left.every((value) => rightValues.has(value));
}

function paginationItems(currentPage, totalPages) {
  if (totalPages <= 7) {
    return Array.from({ length: totalPages }, (_, index) => index + 1);
  }

  const pageNumbers = new Set([1, totalPages, currentPage - 1, currentPage, currentPage + 1]);
  if (currentPage <= 3) {
    [2, 3, 4].forEach((value) => pageNumbers.add(value));
  }
  if (currentPage >= totalPages - 2) {
    [totalPages - 3, totalPages - 2, totalPages - 1].forEach((value) => pageNumbers.add(value));
  }

  const pages = [...pageNumbers]
    .filter((value) => value >= 1 && value <= totalPages)
    .sort((left, right) => left - right);
  const items = [];
  pages.forEach((value, index) => {
    if (index > 0 && value - pages[index - 1] > 1) {
      items.push(`gap-${pages[index - 1]}-${value}`);
    }
    items.push(value);
  });
  return items;
}

function formatReviewTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }).format(date);
}

function resolveRelativePath(baseFilePath, relativePath) {
  const segments = baseFilePath.split("/");
  segments.pop();
  for (const segment of relativePath.split("/")) {
    if (!segment || segment === ".") continue;
    if (segment === "..") {
      segments.pop();
    } else {
      segments.push(segment);
    }
  }
  return segments.join("/");
}

function MultiSelectFilter({ label, compactLabel = label, options, selectedValues, onChange, disabled = false }) {
  const selected = new Set(selectedValues);
  const allSelected = options.length > 0 && selected.size === options.length;

  function toggleValue(value) {
    const next = new Set(selectedValues);
    if (next.has(value)) {
      next.delete(value);
    } else {
      next.add(value);
    }
    onChange(options.filter((option) => next.has(option)));
  }

  return (
    <details
      className={`my-tasks-multiselect${disabled ? " is-disabled" : ""}`}
      aria-disabled={disabled}
    >
      <summary
        tabIndex={disabled ? -1 : undefined}
        aria-label={label}
        title={disabled ? "仅适用于我的任务" : undefined}
        onClick={(event) => {
          if (disabled) event.preventDefault();
        }}
      >
        <span className="my-tasks-label-full" aria-hidden="true">{label}</span>
        <span className="my-tasks-label-compact" aria-hidden="true">{compactLabel}</span>
        {!allSelected ? <small>{selected.size}</small> : null}
      </summary>
      <div className="my-tasks-multiselect-menu">
        <div className="my-tasks-multiselect-actions">
          <button type="button" onClick={() => onChange(options)}>全选</button>
          <button type="button" onClick={() => onChange([])}>清空</button>
        </div>
        <div className="my-tasks-multiselect-options">
          {options.map((option) => (
            <label key={option}>
              <input
                type="checkbox"
                checked={selected.has(option)}
                onChange={() => toggleValue(option)}
              />
              <span>{option}</span>
            </label>
          ))}
        </div>
      </div>
    </details>
  );
}

function TaskDetailDialog({ task, onClose }) {
  const [file, setFile] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(false);
  const [editAccess, setEditAccess] = useState(null);
  const [saveMessage, setSaveMessage] = useState("");

  useEffect(() => {
    if (!task) return undefined;
    let disposed = false;

    setFile(null);
    setLoading(true);
    setError("");
    setEditing(false);
    setSaveMessage("");
    getKnowledgeFile("requirements", task.source_path)
      .then((payload) => {
        if (!disposed) setFile(payload);
      })
      .catch((nextError) => {
        if (!disposed) setError(nextError.message || "任务详情加载失败。");
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });

    return () => {
      disposed = true;
    };
  }, [task]);

  useEffect(() => {
    let disposed = false;
    if (!task) {
      setEditAccess(null);
      return () => {
        disposed = true;
      };
    }

    setEditAccess(null);
    getRequirementClickUpEditAccess(task.source_path)
      .then((payload) => {
        if (!disposed) setEditAccess(payload);
      })
      .catch(() => {
        if (!disposed) {
          setEditAccess({
            path: task.source_path,
            can_edit: false,
            reason: "权限检查未完成，当前内容按只读处理，请稍后重试。"
          });
        }
      });

    return () => {
      disposed = true;
    };
  }, [task]);

  useEffect(() => {
    if (!task) return undefined;

    function handleKeyDown(event) {
      if (event.key === "Escape") onClose();
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [task, onClose]);

  if (!task) return null;

  const resolveImageSrc = (src) => {
    if (!src || /^(?:[a-z]+:)?\/\//i.test(src) || src.startsWith("data:") || src.startsWith("/")) {
      return src;
    }
    return buildKnowledgeAssetUrl(
      "requirements",
      resolveRelativePath(task.source_path, src)
    );
  };

  function handleSaved(payload) {
    setEditing(false);
    setSaveMessage(payload.message || "已保存到 ClickUp");
    if (!payload.local_synced) return;
    getKnowledgeFile("requirements", task.source_path, { force: true })
      .then((nextFile) => setFile(nextFile))
      .catch(() => setSaveMessage("已保存到 ClickUp，但详情刷新失败；重新打开后可查看最新内容。"));
  }

  return (
    <div className="task-review-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="task-review-dialog task-detail-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="task-detail-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="task-review-head">
          <div>
            <span id="task-detail-title" className="task-review-eyebrow">任务详情</span>
            <div className="task-detail-meta">
              <a href={task.clickup_url} target="_blank" rel="noreferrer">{task.task_id}</a>
              <Badge tone={statusTone(task.status)}>{task.status}</Badge>
            </div>
          </div>
          <div className="task-review-head-actions">
            {!editing && editAccess?.can_edit ? (
              <button type="button" className="button button-primary" onClick={() => setEditing(true)}>
                编辑
              </button>
            ) : null}
            {!editing && editAccess && !editAccess.can_edit ? (
              <span
                className="clickup-content-readonly"
                role="img"
                aria-label="当前内容只读"
                data-tooltip={editAccess.reason || "当前内容只读"}
                title={editAccess.reason || "当前内容只读"}
                tabIndex={0}
              >
                <ReadOnlyDocumentIcon />
              </span>
            ) : null}
            <button type="button" className="button button-secondary" onClick={onClose} autoFocus>
              关闭
            </button>
          </div>
        </header>
        <div className="task-review-body task-detail-body">
          {editing ? (
            <Suspense fallback={<div className="clickup-content-editor-state">正在加载编辑器…</div>}>
              <ClickUpContentEditor
                path={task.source_path}
                resolveImageSrc={resolveImageSrc}
                onCancel={() => setEditing(false)}
                onSaved={handleSaved}
              />
            </Suspense>
          ) : null}
          {!editing && saveMessage ? (
            <div className="clickup-content-save-message" role="status">{saveMessage}</div>
          ) : null}
          {!editing && loading ? (
            <div className="task-review-state">
              <span className="task-review-spinner" aria-hidden="true" />
              <strong>正在加载任务详情…</strong>
            </div>
          ) : null}
          {!editing && error ? <div className="task-review-error">{error}</div> : null}
          {!editing && file ? (
            <DocumentDetailPanel
              node={{
                kind: "file",
                name: task.source_path.split("/").pop(),
                path: task.source_path,
                title: task.title
              }}
              fileContent={file.content}
              fileContentFormat={file.content_type}
              inlineFileHeader
              resolveImageSrc={resolveImageSrc}
              clickUpCommentsPath={task.source_path}
            />
          ) : null}
        </div>
      </section>
    </div>
  );
}

function ReviewDialog({ target, onClose }) {
  const [jobs, setJobs] = useState([]);
  const [selectedJobId, setSelectedJobId] = useState("");
  const [review, setReview] = useState(null);
  const [loading, setLoading] = useState(true);
  const [reviewLoading, setReviewLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!target) return undefined;
    let disposed = false;
    let timerId = null;

    async function loadReviews() {
      setLoading(true);
      setError("");
      setJobs([]);
      setSelectedJobId("");
      setReview(null);
      try {
        const payload = await listCodeReviewJobs(target.repoFullName, target.prNumber);
        if (disposed) return;
        let nextJobs = payload.jobs || [];
        const currentJob = target.reviewJob || nextJobs[0];
        if (!currentJob || currentJob.status === "failed") {
          const createdJob = await createManualCodeReview(target.repoFullName, target.prNumber);
          if (disposed) return;
          nextJobs = [createdJob, ...nextJobs];
        }
        setJobs(nextJobs);
        let activeJob = nextJobs[0];
        while (!disposed && activeJob && ["pending", "running"].includes(activeJob.status)) {
          await new Promise((resolve) => {
            timerId = window.setTimeout(resolve, 2200);
          });
          if (disposed) return;
          activeJob = await getCodeReviewJob(activeJob.job_id);
          if (disposed) return;
          setJobs((current) => current.map((job) => job.job_id === activeJob.job_id ? activeJob : job));
        }
        if (activeJob?.status === "completed" && activeJob.review_id) {
          setSelectedJobId(activeJob.job_id);
          setReviewLoading(true);
          const detail = await getCodeReview(activeJob.review_id);
          if (!disposed) setReview(detail);
        } else if (activeJob?.status === "failed") {
          throw new Error(activeJob.error_message || "代码审核失败。");
        }
      } catch (nextError) {
        if (!disposed) setError(nextError.message || "审核记录加载失败。");
      } finally {
        if (!disposed) {
          setLoading(false);
          setReviewLoading(false);
        }
      }
    }

    void loadReviews();
    return () => {
      disposed = true;
      if (timerId) window.clearTimeout(timerId);
    };
  }, [target]);

  async function handleSelectJob(job) {
    if (!job.review_id || job.status !== "completed" || job.job_id === selectedJobId) return;
    setSelectedJobId(job.job_id);
    setReview(null);
    setReviewLoading(true);
    setError("");
    try {
      setReview(await getCodeReview(job.review_id));
    } catch (nextError) {
      setError(nextError.message || "审核报告加载失败。");
    } finally {
      setReviewLoading(false);
    }
  }

  if (!target) return null;

  return (
    <div className="task-review-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="task-review-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="task-review-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="task-review-head">
          <div>
            <span className="task-review-eyebrow">审核记录</span>
            <h2 id="task-review-title">
              {shortRepoName(target.repoFullName)} #{target.prNumber}
            </h2>
          </div>
          <button type="button" className="button button-secondary" onClick={onClose}>关闭</button>
        </header>
        <div className="task-review-body">
          {loading ? (
            <div className="task-review-state">
              <span className="task-review-spinner" aria-hidden="true" />
              <strong>正在读取审核记录…</strong>
            </div>
          ) : null}
          {!loading && jobs.length === 0 && !error ? (
            <div className="task-review-empty">
              <strong>暂无审核记录</strong>
              <p>审核会由系统自动发起，生成后可在这里查看。</p>
            </div>
          ) : null}
          {!loading && jobs.length > 0 ? (
            <div className="task-review-layout">
              <div className="task-review-list" aria-label="审核记录列表">
                {jobs.map((job) => {
                  const status = REVIEW_STATUS[job.status] || { label: job.status, tone: "neutral" };
                  const selectable = job.status === "completed" && Boolean(job.review_id);
                  return (
                    <button
                      type="button"
                      key={job.job_id}
                      className={`task-review-list-item${job.job_id === selectedJobId ? " is-active" : ""}`}
                      disabled={!selectable || reviewLoading}
                      onClick={() => void handleSelectJob(job)}
                    >
                      <span>
                        <strong>{formatReviewTime(job.created_at) || job.job_id}</strong>
                        <small>{job.head_sha?.slice(0, 7) || "未记录提交"}</small>
                      </span>
                      <Badge tone={status.tone}>{status.label}</Badge>
                    </button>
                  );
                })}
              </div>
              <div className="task-review-report">
                {reviewLoading ? (
                  <div className="task-review-state">
                    <span className="task-review-spinner" aria-hidden="true" />
                    <strong>正在加载审核报告…</strong>
                  </div>
                ) : null}
                {!reviewLoading && !review ? (
                  <div className="task-review-empty">
                    <strong>当前记录暂无报告</strong>
                    <p>选择一条已完成的审核记录查看报告。</p>
                  </div>
                ) : null}
                {review ? <MarkdownRenderer markdown={review.content} variant="document" /> : null}
              </div>
            </div>
          ) : null}
          {error ? <div className="task-review-error">{error}</div> : null}
        </div>
      </section>
    </div>
  );
}

export default function MyTasksPage({ onReferenceNode, onNavigateAssistant, onNavigateKnowledgeNode }) {
  const pageRootRef = useRef(null);
  const [tasks, setTasks] = useState([]);
  const [environmentOptions, setEnvironmentOptions] = useState([]);
  const [statuses, setStatuses] = useState([]);
  const [activeStatuses, setActiveStatuses] = useState([]);
  const [completedStatuses, setCompletedStatuses] = useState([]);
  const [roles, setRoles] = useState([]);
  const [statusPreset, setStatusPreset] = useState("active");
  const [assignmentScope, setAssignmentScope] = useState("mine");
  const [customStatuses, setCustomStatuses] = useState([]);
  const [rolePreset, setRolePreset] = useState("primary");
  const [customRoles, setCustomRoles] = useState([]);
  const [query, setQuery] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(0);
  const [scopeCounts, setScopeCounts] = useState({ active: 0, completed: 0, all: 0 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [environmentSaving, setEnvironmentSaving] = useState("");
  const [detailTarget, setDetailTarget] = useState(null);
  const [assistantPreparing, setAssistantPreparing] = useState("");
  const [reviewTarget, setReviewTarget] = useState(null);

  useEffect(() => {
    const timerId = window.setTimeout(() => {
      setPage(1);
      setSearchQuery(query.trim());
    }, 250);
    return () => window.clearTimeout(timerId);
  }, [query]);

  useEffect(() => {
    let disposed = false;
    const hasEmptyCustomStatus = statusPreset === "custom" && customStatuses.length === 0;
    const hasEmptyCustomRole =
      assignmentScope === "mine" && rolePreset === "custom" && customRoles.length === 0;
    if (hasEmptyCustomStatus || hasEmptyCustomRole) {
      setTasks([]);
      setTotal(0);
      setTotalPages(0);
      setLoading(false);
      return () => {
        disposed = true;
      };
    }

    setLoading(true);
    setError("");
    listMyTasks({
      scope: statusPreset === "custom" ? "all" : statusPreset,
      assignmentScope,
      page,
      pageSize: PAGE_SIZE,
      query: searchQuery,
      statuses: statusPreset === "custom" ? customStatuses : null,
      roles: assignmentScope === "mine" && rolePreset === "custom" ? customRoles : null
    })
      .then((payload) => {
        if (disposed) return;
        setTasks(payload.tasks || []);
        setEnvironmentOptions(payload.environment_options || []);
        setStatuses(payload.status_options || []);
        setActiveStatuses(payload.active_status_options || []);
        setCompletedStatuses(payload.completed_status_options || []);
        setRoles(payload.role_options || []);
        setTotal(payload.total || 0);
        setTotalPages(payload.total_pages || 0);
        setScopeCounts(payload.scope_counts || { active: 0, completed: 0, all: 0 });
        if (payload.page && payload.page !== page) setPage(payload.page);
      })
      .catch((nextError) => {
        if (!disposed) setError(nextError.message || "任务加载失败。");
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });
    return () => {
      disposed = true;
    };
  }, [assignmentScope, statusPreset, customStatuses, rolePreset, customRoles, searchQuery, page]);

  const orderedRoles = useMemo(() => {
    const found = new Set(roles);
    return [
      ...ROLE_ORDER.filter((role) => found.has(role)),
      ...roles.filter((role) => !ROLE_ORDER.includes(role)).sort()
    ];
  }, [roles]);
  const primaryRoles = useMemo(
    () => orderedRoles.filter((role) => role !== "抄送人"),
    [orderedRoles]
  );
  const selectedStatuses = useMemo(() => {
    if (statusPreset === "active") return activeStatuses;
    if (statusPreset === "completed") return completedStatuses;
    if (statusPreset === "all") return statuses;
    return customStatuses;
  }, [statusPreset, activeStatuses, completedStatuses, statuses, customStatuses]);
  const selectedRoles = assignmentScope === "mine"
    ? (rolePreset === "primary" ? primaryRoles : customRoles)
    : [];

  function applyStatusSelection(values) {
    setPage(1);
    if (sameSelection(values, activeStatuses)) {
      setStatusPreset("active");
    } else if (sameSelection(values, completedStatuses)) {
      setStatusPreset("completed");
    } else if (sameSelection(values, statuses)) {
      setStatusPreset("all");
    } else {
      setCustomStatuses(values);
      setStatusPreset("custom");
    }
  }

  function applyRoleSelection(values) {
    setPage(1);
    if (sameSelection(values, primaryRoles)) {
      setRolePreset("primary");
    } else {
      setCustomRoles(values);
      setRolePreset("custom");
    }
  }

  function changePage(nextPage) {
    if (nextPage < 1 || nextPage > totalPages || nextPage === page) return;
    setPage(nextPage);
    pageRootRef.current?.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function handleEnvironmentChange(task, environment) {
    setEnvironmentSaving(task.task_id);
    setError("");
    try {
      await updateMyTaskEnvironment(task.task_id, environment);
      setTasks((current) =>
        current.map((item) =>
          item.task_id === task.task_id
            ? { ...item, manual_environment: environment || null }
            : item
        )
      );
    } catch (nextError) {
      setError(nextError.message || "测试环境保存失败。");
    } finally {
      setEnvironmentSaving("");
    }
  }

  async function handleCreateAssistantSession(task, pr) {
    const targetKey = `${task.task_id}:${pr.repo_full_name}#${pr.pr_number}`;
    setAssistantPreparing(targetKey);
    setError("");
    try {
      const payload = await createMyTaskAssistantSession(
        task.task_id,
        pr.repo_full_name,
        pr.pr_number
      );
      onNavigateAssistant?.({
        sessionId: payload.session_id,
        reference: buildTaskRequirementReference(task)
      });
    } catch (nextError) {
      setError(nextError.message || "AI Assistant 会话准备失败。");
    } finally {
      setAssistantPreparing("");
    }
  }

  return (
    <div className="my-tasks-page" ref={pageRootRef}>
      <section className="my-tasks-toolbar" aria-label="任务筛选">
        <div className="my-tasks-filter-group">
          <div className="my-tasks-scope">
            {[
              ["active", "进行中", "进行"],
              ["completed", "已完成", "完成"],
              ["all", "全部", "全部"]
            ].map(([value, label, compactLabel]) => (
              <button
                type="button"
                key={value}
                className={statusPreset === value ? "is-active" : ""}
                aria-label={`${label}，${scopeCounts[value] || 0} 个任务`}
                onClick={() => {
                  setPage(1);
                  setStatusPreset(value);
                }}
              >
                <span className="my-tasks-label-full" aria-hidden="true">{label}</span>
                <span className="my-tasks-label-compact" aria-hidden="true">{compactLabel}</span>
                <small>{scopeCounts[value] || 0}</small>
              </button>
            ))}
          </div>
          <MultiSelectFilter
            label="状态"
            options={statuses}
            selectedValues={selectedStatuses}
            onChange={applyStatusSelection}
          />
        </div>
        <input
          className="my-tasks-search"
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="搜索任务 / ID / PR"
          aria-label="搜索任务"
        />
        <div className="my-tasks-filter-group">
          <button
            type="button"
            className={`my-tasks-audience-button${assignmentScope === "mine" ? " is-active" : ""}`}
            aria-label="我的任务"
            onClick={() => {
              setPage(1);
              setAssignmentScope("mine");
            }}
          >
            <span className="my-tasks-label-full" aria-hidden="true">我的任务</span>
            <span className="my-tasks-label-compact" aria-hidden="true">我的</span>
          </button>
          <MultiSelectFilter
            label="我的角色"
            compactLabel="角色"
            options={orderedRoles}
            selectedValues={selectedRoles}
            onChange={applyRoleSelection}
            disabled={assignmentScope === "all"}
          />
          <button
            type="button"
            className={`my-tasks-audience-button${assignmentScope === "all" ? " is-active" : ""}`}
            aria-label="所有人"
            onClick={() => {
              setPage(1);
              setAssignmentScope("all");
            }}
          >
            <span className="my-tasks-label-full" aria-hidden="true">所有人</span>
            <span className="my-tasks-label-compact" aria-hidden="true">全员</span>
          </button>
        </div>
        <span className="my-tasks-count" aria-label={`共 ${total} 个任务`}>
          {total}<span className="my-tasks-count-suffix"> 个任务</span>
        </span>
      </section>

      {error ? <div className="my-tasks-error">{error}</div> : null}

      <section className="my-tasks-list" aria-live="polite">
        <div className="my-tasks-list-head" aria-hidden="true">
          <span>任务</span>
          <span>状态</span>
          <span>设计</span>
          <span>关联 PR</span>
          <span>测试环境</span>
        </div>
        {loading ? <div className="my-tasks-empty">正在加载任务…</div> : null}
        {!loading && tasks.length === 0 ? (
          <div className="my-tasks-empty">当前筛选条件下没有任务。</div>
        ) : null}
        {!loading && tasks.map((task) => (
          <article className="my-task-row" key={task.task_id}>
            <div className="my-task-main">
              <div className="my-task-heading">
                <button
                  type="button"
                  className="my-task-title"
                  onClick={() => setDetailTarget(task)}
                >
                  {task.title}
                </button>
                <button
                  type="button"
                  className="toolbar-icon-button document-reference-button my-task-reference"
                  aria-label={`引用任务 ${task.title} 到 AI 对话`}
                  title="引用任务到对话"
                  onClick={() => onReferenceNode?.(buildTaskRequirementReference(task))}
                >
                  <ReferenceIcon />
                </button>
                <button
                  type="button"
                  className="review-entry-button my-task-requirement-review"
                  aria-label={`评审需求 ${task.title}`}
                  title="评审需求"
                  onClick={() => onNavigateKnowledgeNode?.(task.source_path, { reviewAction: "open" })}
                >
                  评
                </button>
              </div>
              <div className="my-task-meta">
                <a href={task.clickup_url} target="_blank" rel="noreferrer">{task.task_id}</a>
                {task.priority && task.priority !== "未设置" ? <span>{task.priority}</span> : null}
                {(task.relations || []).map((relation) => (
                  <span key={relation}>{relation}</span>
                ))}
              </div>
            </div>
            <div className="my-task-status">
              <Badge tone={statusTone(task.status)}>{task.status}</Badge>
            </div>
            <div className="my-task-design">
              {!(task.designers || []).length ? (
                <span className="my-task-muted">--</span>
              ) : task.figma_url ? (
                <a
                  className="my-task-design-link"
                  href={task.figma_url}
                  target="_blank"
                  rel="noreferrer"
                  aria-label={`在 Figma 中查看 ${task.title} 的设计稿`}
                >
                  <span>{(task.designers || []).join("、")}</span>
                  <small>{formatReviewTime(task.updated_at)}</small>
                </a>
              ) : (
                <span className="my-task-designers">{task.designers.join("、")}</span>
              )}
            </div>
            <div className="my-task-prs">
              {(task.pull_requests || []).length === 0 ? (
                <span className="my-task-muted">暂无关联 PR</span>
              ) : task.pull_requests.map((pr) => {
                const prStatus = prStatusMeta(pr.state);
                const reviewStatus = REVIEW_STATUS[pr.review_job?.status];
                const targetKey = `${task.task_id}:${pr.repo_full_name}#${pr.pr_number}`;
                const preparing = assistantPreparing === targetKey;
                return (
                  <div className="my-task-pr" key={`${pr.repo_full_name}#${pr.pr_number}`}>
                    <span className="my-task-pr-main">
                      <a href={pr.pr_url} target="_blank" rel="noreferrer">
                        {shortRepoName(pr.repo_full_name)} #{pr.pr_number}
                      </a>
                      <Badge tone={prStatus.tone}>{prStatus.label}</Badge>
                    </span>
                    <span className="my-task-pr-actions">
                      <button
                        type="button"
                        className="review-entry-button my-task-pr-review"
                        title={reviewStatus?.label === "已审核" ? "查看代码审核" : "审核代码"}
                        aria-label={`${reviewStatus?.label === "已审核" ? "查看" : "审核"} ${shortRepoName(pr.repo_full_name)} #${pr.pr_number} 的代码`}
                        onClick={() =>
                          setReviewTarget({
                            repoFullName: pr.repo_full_name,
                            prNumber: pr.pr_number,
                            reviewJob: pr.review_job
                          })
                        }
                      >
                        审
                      </button>
                      <button
                        type="button"
                        className="toolbar-icon-button document-reference-button my-task-pr-reference"
                        disabled={Boolean(assistantPreparing)}
                        aria-busy={preparing ? "true" : undefined}
                        aria-label={`引用 ${shortRepoName(pr.repo_full_name)} #${pr.pr_number} 到 AI 对话`}
                        title={preparing ? "正在引用…" : "引用 PR 到对话"}
                        onClick={() => void handleCreateAssistantSession(task, pr)}
                      >
                        <ReferenceIcon />
                      </button>
                    </span>
                  </div>
                );
              })}
            </div>
            <div className="my-task-environment">
              <select
                value={currentEnvironment(task)}
                disabled={
                  environmentSaving === task.task_id
                  || (assignmentScope === "all" && !(task.relations || []).length)
                }
                title={
                  assignmentScope === "all" && !(task.relations || []).length
                    ? "只能调整自己关联的任务"
                    : undefined
                }
                onChange={(event) => void handleEnvironmentChange(task, event.target.value)}
                aria-label={`调整 ${task.title} 的测试环境`}
              >
                <option value="">无</option>
                {environmentOptions.map((environment) => (
                  <option key={environment} value={environment}>{environment}</option>
                ))}
              </select>
              {task.manual_environment ? <small>人工调整</small> : task.detected_environments?.length ? <small>评论识别</small> : null}
            </div>
          </article>
        ))}
      </section>

      {!loading && totalPages > 1 ? (
        <nav className="my-tasks-pagination" aria-label="任务分页">
          <button
            type="button"
            disabled={page <= 1}
            onClick={() => changePage(page - 1)}
          >
            上一页
          </button>
          <div className="my-tasks-pagination-pages">
            {paginationItems(page, totalPages).map((item) =>
              typeof item === "string" ? (
                <span key={item} aria-hidden="true">…</span>
              ) : (
                <button
                  type="button"
                  key={item}
                  className={item === page ? "is-active" : ""}
                  aria-current={item === page ? "page" : undefined}
                  onClick={() => changePage(item)}
                >
                  {item}
                </button>
              )
            )}
          </div>
          <button
            type="button"
            disabled={page >= totalPages}
            onClick={() => changePage(page + 1)}
          >
            下一页
          </button>
        </nav>
      ) : null}

      <TaskDetailDialog
        task={detailTarget}
        onClose={() => setDetailTarget(null)}
      />

      <ReviewDialog
        target={reviewTarget}
        onClose={() => setReviewTarget(null)}
      />
    </div>
  );
}
