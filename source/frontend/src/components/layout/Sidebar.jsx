import Card from "../common/Card";
import BrandLogo from "../branding/BrandLogo";
import VibeHubLauncher from "./VibeHubLauncher";
import SessionDeleteButton from "../common/SessionDeleteButton";
import { navItems, workspaceByPage } from "../../data/prototypeData";
import { visibleKnowledgeScopes } from "../../data/knowledgeScopes";
import { useEffect, useMemo, useRef, useState } from "react";
import ActionIcon from "../common/ActionIcon";
import { getImageGenerationQuota } from "../../services/assistantApi";
import DocoSettingsDialog from "../common/DocoSettingsDialog";

function SessionTitle({ title, running = false, className = "" }) {
  const safeTitle = title || "未命名会话";

  if (!running) {
    return <strong className={className}>{safeTitle}</strong>;
  }

  const prefix = safeTitle.slice(0, 2) || safeTitle;
  const rest = safeTitle.slice(prefix.length);

  return (
    <strong className={`${className} session-title-running`.trim()}>
      <span className="processing-text session-title-text">
        <span className="processing-text-highlight">{prefix}</span>
        {rest ? <span className="processing-text-rest">{rest}</span> : null}
      </span>
      <span className="sr-only">，该会话正在执行</span>
    </strong>
  );
}

function buildParams(item) {
  if (item.page === "assistant") {
    return {};
  }
  if (item.page === "knowledge") {
    return { type: "requirements" };
  }
  if (item.page === "skill") {
    return {};
  }
  if (item.page === "shared") {
    return {};
  }
  return {};
}

function formatResetTime(resetsAt) {
  if (!resetsAt) {
    return "";
  }
  const date = new Date(resetsAt);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function QuotaRow({ label, window: quotaWindow }) {
  const limit = Number(quotaWindow?.limit || 0);
  const used = Number(quotaWindow?.used || 0);
  if (limit <= 0) {
    return (
      <div className="user-quota-row">
        <div className="user-quota-row-head">
          <span>{label}</span>
          <span className="user-quota-count">不限制</span>
        </div>
      </div>
    );
  }
  const ratio = Math.min(1, used / limit);
  const exhausted = used >= limit;
  const warn = !exhausted && ratio >= 0.8;
  const stateClass = exhausted ? "is-exhausted" : warn ? "is-warn" : "";
  const resetText = formatResetTime(quotaWindow?.resets_at);
  return (
    <div className={`user-quota-row ${stateClass}`.trim()}>
      <div className="user-quota-row-head">
        <span>{label}</span>
        <span className="user-quota-count">
          {used} / {limit}
        </span>
      </div>
      <div className="user-quota-meter" aria-hidden="true">
        <span className="user-quota-meter-fill" style={{ width: `${ratio * 100}%` }} />
      </div>
      {resetText ? (
        <div className="user-quota-reset">{exhausted ? `${resetText} 重置` : `重置 ${resetText}`}</div>
      ) : null}
    </div>
  );
}

export default function Sidebar({
  activePage,
  route,
  assistantSessions,
  sharedAssistantSessions = [],
  activeAssistantSessionIds = [],
  currentUser,
  requirementReviewBadge,
  expanded,
  onToggleSidebar,
  onNavigate,
  onDeleteAssistantSession,
  onLogout
}) {
  const workspace = workspaceByPage[activePage];
  const currentSessionId = route?.sessionId || "";
  const assistantView = route?.view || "chat";
  const routeHistoryQuery = route?.q || "";
  const currentKnowledgeType = route?.type || "requirements";
  const userName = currentUser?.name || currentUser?.email || "User";
  const userAvatar = userName.trim().charAt(0).toUpperCase();
  const [sessionSearch, setSessionSearch] = useState(routeHistoryQuery);
  const [deleteError, setDeleteError] = useState("");
  const [deletingSessionId, setDeletingSessionId] = useState("");
  const activeSessionIdSet = useMemo(() => new Set(activeAssistantSessionIds), [activeAssistantSessionIds]);

  useEffect(() => {
    setSessionSearch(routeHistoryQuery);
  }, [routeHistoryQuery]);

  const [quotaOpen, setQuotaOpen] = useState(false);
  const [quota, setQuota] = useState(null);
  const [quotaLoading, setQuotaLoading] = useState(false);
  const [quotaError, setQuotaError] = useState("");
  const [popoverStyle, setPopoverStyle] = useState(null);
  const quotaPanelRef = useRef(null);
  const [docoSettingsOpen, setDocoSettingsOpen] = useState(false);

  useEffect(() => {
    function handleDocoShortcut(event) {
      if (
        event.isComposing ||
        event.repeat ||
        !(event.metaKey || event.ctrlKey) ||
        !event.altKey ||
        !event.shiftKey ||
        event.key.toLowerCase() !== "d"
      ) {
        return;
      }
      event.preventDefault();
      setQuotaOpen(false);
      setDocoSettingsOpen(true);
    }

    window.addEventListener("keydown", handleDocoShortcut);
    return () => window.removeEventListener("keydown", handleDocoShortcut);
  }, []);

  useEffect(() => {
    if (!quotaOpen) {
      return;
    }
    let cancelled = false;
    setQuotaLoading(true);
    setQuotaError("");
    getImageGenerationQuota()
      .then((data) => {
        if (!cancelled) {
          setQuota(data);
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setQuotaError(error.message || "加载额度失败");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setQuotaLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [quotaOpen]);

  useEffect(() => {
    if (!quotaOpen) {
      return;
    }
    function handlePointerDown(event) {
      if (quotaPanelRef.current && !quotaPanelRef.current.contains(event.target)) {
        setQuotaOpen(false);
      }
    }
    function handleKeyDown(event) {
      if (event.key === "Escape") {
        setQuotaOpen(false);
      }
    }
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [quotaOpen]);

  function toggleQuotaPanel(event) {
    setQuotaOpen((open) => {
      const next = !open;
      if (next) {
        const rect = event.currentTarget.getBoundingClientRect();
        setPopoverStyle({
          left: `${rect.right + 10}px`,
          bottom: `${Math.max(12, window.innerHeight - rect.bottom)}px`
        });
      } else {
        setPopoverStyle(null);
      }
      return next;
    });
  }

  const filteredSessions = useMemo(() => {
    const normalizedKeyword = sessionSearch.trim().toLowerCase();
    const sharedBySessionId = new Map(
      sharedAssistantSessions.map((item) => [item.session?.session_id, item]).filter(([sessionId]) => Boolean(sessionId))
    );
    const ownSessionIds = new Set(assistantSessions.map((session) => session.session_id));
    const ownItems = assistantSessions.map((session) => {
      const shared = sharedBySessionId.get(session.session_id) || null;
      return {
        id: `own:${session.session_id}`,
        kind: "own",
        session,
        shared,
        shareDirection: shared ? "outgoing" : "private",
        sortAt: session.updated_at
      };
    });
    const sharedItems = sharedAssistantSessions
      .filter((item) => !ownSessionIds.has(item.session?.session_id))
      .map((item) => ({
        id: `shared:${item.share_id}`,
        kind: "shared",
        session: item.session,
        shared: item,
        shareDirection: "incoming",
        sortAt: item.created_at || item.session?.updated_at
      }));

    return ownItems
      .concat(sharedItems)
      .filter((item) => {
        if (!normalizedKeyword) {
          return true;
        }
        const title = item.session?.title?.toLowerCase() || "";
        const preview = item.session?.last_message_preview?.toLowerCase() || "";
        const owner = item.shared?.owner?.name?.toLowerCase() || item.shared?.owner?.email?.toLowerCase() || "";
        return title.includes(normalizedKeyword) || preview.includes(normalizedKeyword) || owner.includes(normalizedKeyword);
      })
      .sort((a, b) => new Date(b.sortAt || 0).getTime() - new Date(a.sortAt || 0).getTime());
  }, [assistantSessions, sessionSearch, sharedAssistantSessions]);

  function handleOpenHistoryPage(keyword = sessionSearch) {
    onNavigate("assistant", {
      view: "history",
      q: keyword.trim() || undefined
    });
  }

  async function handleDeleteSession(event, session) {
    event.stopPropagation();

    if (!onDeleteAssistantSession || deletingSessionId) {
      return;
    }

    const confirmed = window.confirm(`确认删除会话“${session.title}”吗？此操作不可恢复。`);
    if (!confirmed) {
      return;
    }

    setDeleteError("");
    setDeletingSessionId(session.session_id);
    try {
      await onDeleteAssistantSession(session.session_id);
    } catch (error) {
      setDeleteError(error.message);
    } finally {
      setDeletingSessionId("");
    }
  }

  return (
    <aside className="layout-sidebar">
      <div className="sidebar-header">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">
            <BrandLogo className="brand-mark-image" />
          </div>
          <div className="brand-copy" aria-hidden={!expanded}>
            <h1>ai-prd</h1>
          </div>
        </div>
        <div className="sidebar-header-actions">
          <VibeHubLauncher />
          <button
            type="button"
            className={`icon-button sidebar-toggle ${expanded ? "sidebar-toggle-expanded" : "sidebar-toggle-collapsed"}`.trim()}
            onClick={onToggleSidebar}
            aria-label={expanded ? "收起侧边导航" : "展开侧边导航"}
          >
            <span className="sidebar-toggle-logo" aria-hidden={expanded}>
              <BrandLogo className="sidebar-toggle-logo-image" />
            </span>
            <span className="icon-button-glyph">{expanded ? "‹" : "›"}</span>
          </button>
        </div>
      </div>

      <nav className="nav-block">
        {navItems.map((item) => (
          <div key={item.page} className="nav-entry">
            <button
              type="button"
              className={`nav-item ${activePage === item.page ? "nav-item-active" : ""}`}
              onClick={() => onNavigate(item.page, buildParams(item))}
            >
              <span className={`nav-icon nav-icon-${item.tone}`}>{item.short}</span>
              <span className="nav-copy" aria-hidden={!expanded}>
                <strong>{item.label}</strong>
              </span>
            </button>

            {activePage === "knowledge" && item.page === "knowledge" ? (
              <div className="subnav" aria-hidden={!expanded}>
                {visibleKnowledgeScopes.map((scope) => (
                  <button
                    key={scope.id}
                    type="button"
                    className={`subnav-item ${currentKnowledgeType === scope.id ? "subnav-item-active" : ""}`.trim()}
                    aria-label={scope.id === "requirements" && requirementReviewBadge ? `${scope.label}，有未读评审` : scope.label}
                    onClick={() => onNavigate("knowledge", { type: scope.id })}
                    tabIndex={expanded ? 0 : -1}
                  >
                    <span>{scope.label}</span>
                    {scope.id === "requirements" && requirementReviewBadge ? (
                      <span className="review-badge subnav-review-badge" title="有未读评审" aria-hidden="true">
                        评
                      </span>
                    ) : null}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        ))}
      </nav>

      {activePage === "assistant" ? (
        <Card className="sidebar-section sidebar-session-section" aria-hidden={!expanded}>
          <div className="sidebar-session-toolbar">
            <label className="sr-only" htmlFor="sidebar-session-search">
              搜索会话
            </label>
            <input
              id="sidebar-session-search"
              type="search"
              className="sidebar-session-search"
              placeholder="搜索会话"
              value={sessionSearch}
              onChange={(event) => setSessionSearch(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  handleOpenHistoryPage(event.currentTarget.value);
                }
              }}
              tabIndex={expanded ? 0 : -1}
            />
            <button
              type="button"
              className="icon-button sidebar-session-create"
              onClick={() => onNavigate("assistant")}
              aria-label="新建会话，快捷键 Cmd+K"
              title="新建会话 (Cmd+K)"
              tabIndex={expanded ? 0 : -1}
            >
              +
            </button>
          </div>
          <div className="recent-list">
            {filteredSessions.map((item) => (
              <div
                key={item.id}
                className={`recent-session-row ${item.shareDirection === "incoming" ? "recent-session-row-shared-incoming" : ""} ${
                  item.shareDirection === "outgoing" ? "recent-session-row-shared-outgoing" : ""
                }`.trim()}
              >
                <button
                  type="button"
                  className={`recent-item ${
                    (item.kind === "own" && assistantView === "chat" && currentSessionId === item.session.session_id) ||
                    (item.kind === "shared" && route?.page === "shared" && route?.shareToken === item.shared.share_token)
                      ? "recent-item-active"
                      : ""
                  }`.trim()}
                  onClick={() =>
                    item.kind === "shared"
                      ? onNavigate("shared", { shareToken: item.shared.share_token })
                      : onNavigate("assistant", { sessionId: item.session.session_id })
                  }
                  tabIndex={expanded ? 0 : -1}
                >
                  <span className="recent-item-title-row">
                    {item.shareDirection !== "private" ? (
                      <span
                        className={`recent-share-icon ${
                          item.shareDirection === "incoming" ? "recent-share-icon-incoming" : "recent-share-icon-outgoing"
                        }`.trim()}
                        title={item.shareDirection === "incoming" ? "共享给我" : "已共享"}
                        aria-label={item.shareDirection === "incoming" ? "共享给我" : "已共享"}
                      >
                        {item.shareDirection === "incoming" ? "↓" : "↗"}
                      </span>
                    ) : null}
                    <SessionTitle
                      title={item.session.title}
                      running={Boolean(
                        item.session.has_active_turn ||
                        (item.kind === "own" && activeSessionIdSet.has(item.session.session_id))
                      )}
                      className="recent-item-title"
                    />
                  </span>
                </button>
                {item.kind === "own" ? (
                  <SessionDeleteButton
                    title={item.session.title}
                    disabled={!expanded || deletingSessionId === item.session.session_id}
                    onClick={(event) => void handleDeleteSession(event, item.session)}
                  />
                ) : null}
              </div>
            ))}

            {assistantSessions.length === 0 && sharedAssistantSessions.length === 0 ? <p className="folder-empty">暂无历史会话</p> : null}
            {assistantSessions.length + sharedAssistantSessions.length > 0 && filteredSessions.length === 0 ? (
              <p className="folder-empty">最近会话里没有匹配结果，按 Enter 可搜索全部历史。</p>
            ) : null}
            {deleteError ? <p className="folder-empty">{deleteError}</p> : null}
          </div>
          <button
            type="button"
            className={`button button-secondary sidebar-history-button ${
              assistantView === "history" ? "sidebar-history-button-active" : ""
            }`.trim()}
            onClick={() => handleOpenHistoryPage()}
            tabIndex={expanded ? 0 : -1}
          >
            查看全部/收藏
          </button>
        </Card>
      ) : null}

      <div className="sidebar-footer">
        <div className="user-quota" ref={quotaPanelRef}>
          <button
            type="button"
            className="user-card user-card-button"
            aria-expanded={quotaOpen}
            aria-haspopup="dialog"
            title="查看生图额度"
            onClick={toggleQuotaPanel}
          >
            <div className="user-avatar">{userAvatar}</div>
            <div className="user-copy" aria-hidden={!expanded}>
              <strong>{userName}</strong>
              <small>{workspace}</small>
            </div>
            {expanded ? <ActionIcon kind="chart" className="user-quota-icon" /> : null}
          </button>
          {quotaOpen ? (
            <div className="user-quota-popover" style={popoverStyle || undefined} role="dialog" aria-label="生图额度">
              <div className="user-quota-title">生图额度</div>
              {quotaLoading && !quota ? <div className="user-quota-state">加载中…</div> : null}
              {quotaError ? <div className="user-quota-state user-quota-error">{quotaError}</div> : null}
              {quota ? (
                <>
                  <QuotaRow label="今日" window={quota.daily} />
                  <QuotaRow label="本周" window={quota.weekly} />
                </>
              ) : null}
            </div>
          ) : null}
        </div>
        <div className="sidebar-logout">
          <button type="button" className="button button-secondary sidebar-logout-button" onClick={onLogout}>
            退出登录
          </button>
        </div>
      </div>
      <DocoSettingsDialog open={docoSettingsOpen} onClose={() => setDocoSettingsOpen(false)} />
    </aside>
  );
}
