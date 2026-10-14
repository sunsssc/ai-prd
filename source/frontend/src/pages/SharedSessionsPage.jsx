import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import BrandLogo from "../components/branding/BrandLogo";
import MessageCard from "../components/common/MessageCard";
import {
  attachTimelineNotices,
  formatGitScopeSummary,
  toMessageCard
} from "../utils/assistantMessages";
import {
  createSharedAssistantFollowUp,
  createAssistantSessionComment,
  deleteAssistantSessionComment,
  getSharedAssistantSession,
  listAssistantSessionComments,
  listSharedAssistantSessions,
  rebuildSharedAssistantRuntime,
  resumeSharedAssistantTurnStream,
  updateAssistantSessionComment
} from "../services/assistantApi";
import { sanitizeSkillPromptLeak } from "../utils/assistantVisualizations";
import { getShareScopeLabel, shareScopeFromShare } from "../utils/assistantShareScopes";
import {
  createSharedLiveTurnState,
  getSharedTurnPresentation,
  reduceSharedLiveTurnState
} from "../utils/sharedFollowUpState";

const defaultDocumentTitle = "ai-prd Web Prototype";

function formatTime(value) {
  if (!value) {
    return "刚刚";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "刚刚";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }).format(date);
}

function authorName(user) {
  return user?.name || user?.email || "未知用户";
}

function SharedPublicTopBar({ onRequireLogin }) {
  return (
    <header className="shared-public-topbar">
      <div className="shared-public-brand">
        <div className="brand-mark" aria-hidden="true">
          <BrandLogo className="brand-mark-image" />
        </div>
        <span className="shared-public-brand-name">ai-prd</span>
      </div>
      <div className="shared-public-topbar-actions">
        <button type="button" className="button button-secondary" onClick={onRequireLogin}>
          登录
        </button>
      </div>
    </header>
  );
}

function RuntimeRebuildDialog({ request, submitting, error, onCancel, onConfirm }) {
  const confirmRef = useRef(null);

  useEffect(() => {
    confirmRef.current?.focus();
    function handleKeyDown(event) {
      if (event.key === "Escape" && !submitting) {
        onCancel();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onCancel, request.request_id, submitting]);

  if (typeof document === "undefined") {
    return null;
  }

  return createPortal(
    <div className="shared-runtime-rebuild-backdrop" role="presentation">
      <section
        className="shared-runtime-rebuild-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="shared-runtime-rebuild-title"
        aria-describedby="shared-runtime-rebuild-description"
      >
        <p className="assistant-history-eyebrow">Agent Runtime</p>
        <h2 id="shared-runtime-rebuild-title">需要重建会话</h2>
        <p id="shared-runtime-rebuild-description">
          原 Agent Runtime 上下文已失效。重建会创建新的 Runtime，并从已保存的对话记录恢复上下文后重新执行这条追问。
        </p>
        <blockquote>{request.content}</blockquote>
        <p className="shared-runtime-rebuild-note">旧 Runtime 中未持久化的临时状态无法恢复。</p>
        {error ? <p className="form-error">{error}</p> : null}
        <div className="shared-runtime-rebuild-actions">
          <button type="button" className="button button-secondary" disabled={submitting} onClick={onCancel}>
            暂不重建
          </button>
          <button
            ref={confirmRef}
            type="button"
            className="button button-primary"
            disabled={submitting}
            onClick={onConfirm}
          >
            {submitting ? "正在重建" : "重建并重试"}
          </button>
        </div>
      </section>
    </div>,
    document.body
  );
}

export function CommentsBlock({ sessionId, messageId = null, comments, currentUser, onRefresh }) {
  const [draft, setDraft] = useState("");
  const [editingId, setEditingId] = useState("");
  const [editingDraft, setEditingDraft] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit() {
    const content = draft.trim();
    if (!content || submitting) {
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      await createAssistantSessionComment(sessionId, {
        content,
        message_id: messageId
      });
      setDraft("");
      await onRefresh();
    } catch (submitError) {
      setError(submitError.message);
    } finally {
      setSubmitting(false);
    }
  }

  async function handleUpdate(commentId) {
    const content = editingDraft.trim();
    if (!content || submitting) {
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      await updateAssistantSessionComment(commentId, content);
      setEditingId("");
      setEditingDraft("");
      await onRefresh();
    } catch (updateError) {
      setError(updateError.message);
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete(commentId) {
    setSubmitting(true);
    setError("");
    try {
      await deleteAssistantSessionComment(commentId);
      await onRefresh();
    } catch (deleteError) {
      setError(deleteError.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="shared-comments">
      <div className="shared-comment-list">
        {comments.length === 0 ? <p className="shared-comment-empty">暂无评论</p> : null}
        {comments.map((comment) => (
          <article key={comment.comment_id} className="shared-comment">
            <div className="shared-comment-meta">
              <strong>{authorName(comment.user)}</strong>
              <span>{formatTime(comment.updated_at || comment.created_at)}</span>
            </div>
            {editingId === comment.comment_id ? (
              <div className="shared-comment-editor">
                <textarea value={editingDraft} onChange={(event) => setEditingDraft(event.target.value)} />
                <div className="button-row">
                  <button type="button" className="button button-primary" disabled={submitting} onClick={() => handleUpdate(comment.comment_id)}>
                    保存
                  </button>
                  <button type="button" className="button button-secondary" disabled={submitting} onClick={() => setEditingId("")}>
                    取消
                  </button>
                </div>
              </div>
            ) : (
              <p>{comment.content}</p>
            )}
            {comment.can_edit && editingId !== comment.comment_id ? (
              <div className="shared-comment-actions">
                <button
                  type="button"
                  onClick={() => {
                    setEditingId(comment.comment_id);
                    setEditingDraft(comment.content);
                  }}
                >
                  编辑
                </button>
                <button type="button" onClick={() => handleDelete(comment.comment_id)}>
                  删除
                </button>
              </div>
            ) : null}
          </article>
        ))}
      </div>
      <div className="shared-comment-composer">
        <textarea
          value={draft}
          placeholder={currentUser ? "写下评论" : "登录后可评论"}
          disabled={!currentUser || submitting}
          onChange={(event) => setDraft(event.target.value)}
        />
        <button type="button" className="button button-primary" disabled={!draft.trim() || submitting} onClick={handleSubmit}>
          发送
        </button>
      </div>
      {error ? <p className="form-error">{error}</p> : null}
    </div>
  );
}

export default function SharedSessionsPage({ shareToken, currentUser, onNavigateShared, onNavigateCitation, onRequireLogin }) {
  const anonymous = !currentUser;
  const [sharedSessions, setSharedSessions] = useState([]);
  const [sharedDetail, setSharedDetail] = useState(null);
  const [comments, setComments] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [activeCommentTarget, setActiveCommentTarget] = useState(null);
  const [followUpDraft, setFollowUpDraft] = useState("");
  const [followUpSubmitting, setFollowUpSubmitting] = useState(false);
  const [followUpError, setFollowUpError] = useState("");
  const [dismissedRebuildRequestIds, setDismissedRebuildRequestIds] = useState([]);
  const [rebuildSubmitting, setRebuildSubmitting] = useState(false);
  const [rebuildError, setRebuildError] = useState("");
  const [liveTurn, setLiveTurn] = useState(() => createSharedLiveTurnState());

  useEffect(() => {
    setDismissedRebuildRequestIds([]);
    setRebuildSubmitting(false);
    setRebuildError("");
  }, [shareToken]);

  useEffect(() => {
    let disposed = false;

    async function load() {
      setLoading(true);
      setError("");
      setActiveCommentTarget(null);
      try {
        if (shareToken) {
          const payload = await getSharedAssistantSession(shareToken);
          if (disposed) {
            return;
          }
          setSharedDetail(payload);
          setSharedSessions([]);
          const nextComments = await listAssistantSessionComments(payload.share.session.session_id);
          if (!disposed) {
            setComments(nextComments);
          }
        } else {
          const payload = await listSharedAssistantSessions();
          if (!disposed) {
            setSharedSessions(payload);
            setSharedDetail(null);
            setComments([]);
          }
        }
      } catch (loadError) {
        if (!disposed) {
          setError(loadError.message);
          setSharedDetail(null);
          setSharedSessions([]);
          setComments([]);
        }
      } finally {
        if (!disposed) {
          setLoading(false);
        }
      }
    }

    load();
    return () => {
      disposed = true;
    };
  }, [shareToken]);

  useEffect(() => {
    if (!shareToken) {
      return undefined;
    }
    let disposed = false;
    let timer = null;

    async function refreshSharedState() {
      try {
        const payload = await getSharedAssistantSession(shareToken);
        if (!disposed) {
          setSharedDetail(payload);
          setError("");
        }
      } catch (refreshError) {
        if (!disposed) {
          setError(refreshError.message);
          setSharedDetail(null);
        }
      } finally {
        if (!disposed) {
          timer = window.setTimeout(refreshSharedState, 1500);
        }
      }
    }

    timer = window.setTimeout(refreshSharedState, 1500);
    return () => {
      disposed = true;
      if (timer) {
        window.clearTimeout(timer);
      }
    };
  }, [shareToken]);

  useEffect(() => {
    const turnId = sharedDetail?.active_turn?.turn_id || "";
    if (!shareToken || !turnId) {
      setLiveTurn(createSharedLiveTurnState());
      return undefined;
    }

    let disposed = false;
    const controller = new AbortController();

    async function followLiveTurn() {
      while (!disposed) {
        setLiveTurn(createSharedLiveTurnState(turnId));
        try {
          await resumeSharedAssistantTurnStream({
            shareToken,
            turnId,
            signal: controller.signal,
            onOpen: () => {
              if (!disposed) {
                setLiveTurn((current) => ({ ...current, activity: "正在生成实时回复…" }));
              }
            },
            onEvent: (event) => {
              if (!disposed) {
                setLiveTurn((current) => reduceSharedLiveTurnState(current, event));
              }
            }
          });
        } catch (streamError) {
          if (streamError.name === "AbortError" || disposed) {
            return;
          }
          setLiveTurn((current) => ({ ...current, activity: "实时连接中断，正在重新连接…" }));
        }

        if (disposed) {
          return;
        }

        try {
          const payload = await getSharedAssistantSession(shareToken);
          if (disposed) {
            return;
          }
          setSharedDetail(payload);
          if (payload.active_turn?.turn_id !== turnId) {
            return;
          }
        } catch {
          // 详情轮询会继续展示错误；实时流在下一轮尝试重连。
        }

        await new Promise((resolve) => window.setTimeout(resolve, 500));
      }
    }

    void followLiveTurn();
    return () => {
      disposed = true;
      controller.abort();
    };
  }, [shareToken, sharedDetail?.active_turn?.turn_id]);

  useEffect(() => {
    if (!anonymous) {
      return undefined;
    }
    const title = sharedDetail?.share?.session?.title;
    if (title) {
      document.title = `${title} · ai-prd 共享会话`;
    }
    return () => {
      document.title = defaultDocumentTitle;
    };
  }, [anonymous, sharedDetail]);

  const commentsByMessage = useMemo(() => {
    const grouped = new Map();
    for (const comment of comments) {
      const key = comment.message_id || "";
      grouped.set(key, (grouped.get(key) || []).concat(comment));
    }
    return grouped;
  }, [comments]);

  async function refreshComments() {
    const sessionId = sharedDetail?.share?.session?.session_id;
    if (!sessionId) {
      return;
    }
    const payload = await listAssistantSessionComments(sessionId);
    setComments(payload);
  }

  async function handleFollowUpSubmit() {
    const message = followUpDraft.trim();
    if (!message || !currentUser || followUpSubmitting || !shareToken) {
      return;
    }
    setFollowUpSubmitting(true);
    setFollowUpError("");
    try {
      const queued = await createSharedAssistantFollowUp(shareToken, message);
      setFollowUpDraft("");
      setSharedDetail((current) =>
        current
          ? {
              ...current,
              pending_follow_ups: (current.pending_follow_ups || []).some(
                (item) => item.request_id === queued.request_id
              )
                ? current.pending_follow_ups
                : (current.pending_follow_ups || []).concat(queued)
            }
          : current
      );
    } catch (submitError) {
      setFollowUpError(submitError.message);
    } finally {
      setFollowUpSubmitting(false);
    }
  }

  async function handleRuntimeRebuild(request) {
    if (!shareToken || rebuildSubmitting) {
      return;
    }
    setRebuildSubmitting(true);
    setRebuildError("");
    try {
      const running = await rebuildSharedAssistantRuntime(shareToken, request.request_id);
      setDismissedRebuildRequestIds((current) => current.concat(request.request_id));
      setSharedDetail((current) =>
        current
          ? {
              ...current,
              failed_follow_ups: (current.failed_follow_ups || []).filter(
                (item) => item.request_id !== request.request_id
              ),
              pending_follow_ups: (current.pending_follow_ups || []).concat(running)
            }
          : current
      );
    } catch (submitError) {
      setRebuildError(submitError.message);
    } finally {
      setRebuildSubmitting(false);
    }
  }

  const sessionId = sharedDetail?.share?.session?.session_id;
  const sessionComments = commentsByMessage.get("") || [];
  const activeMessage =
    activeCommentTarget?.messageId && sharedDetail
      ? sharedDetail.messages.find((message) => message.message_id === activeCommentTarget.messageId)
      : null;
  const activeComments = activeCommentTarget?.messageId
    ? commentsByMessage.get(activeCommentTarget.messageId) || []
    : sessionComments;
  const commentDrawerTitle = activeCommentTarget?.messageId ? "消息评论" : "会话评论";
  const pendingFollowUps = sharedDetail?.pending_follow_ups || [];
  const runningFollowUp = pendingFollowUps.find((item) => item.status === "running") || null;
  const queuedFollowUps = pendingFollowUps.filter((item) => item.status === "queued");
  const failedFollowUps = currentUser ? sharedDetail?.failed_follow_ups || [] : [];
  const runtimeRebuildFailure = failedFollowUps.find((item) => item.requires_runtime_rebuild) || null;
  const rebuildPrompt = failedFollowUps.find(
    (item) =>
      item.requires_runtime_rebuild &&
      item.can_rebuild_runtime &&
      !dismissedRebuildRequestIds.includes(item.request_id)
  ) || null;
  const turnInProgress = Boolean(sharedDetail?.active_turn || runningFollowUp);
  const activeTurnId = sharedDetail?.active_turn?.turn_id || "";
  const runningTurnPresentation = getSharedTurnPresentation(runningFollowUp?.requested_by, currentUser);
  const followUpLocked = turnInProgress || queuedFollowUps.length > 0 || Boolean(runtimeRebuildFailure);
  const sharedTimeline = sharedDetail
    ? attachTimelineNotices(
        sharedDetail.messages.map((message) => toMessageCard(message, { formatTime })),
        sharedDetail.timeline_notices || [],
        { formatTime }
      )
    : [];
  const liveTurnPreviousActivities = liveTurn.activities
    .filter((activity, index, items) => !(activity === liveTurn.activity && index === items.length - 1))
    .slice(-3);
  const liveTurnSections = liveTurn.skills.length
    ? [
        {
          label: "Skill",
          sources: liveTurn.skills.map((skill) => ({ label: skill.skill_name || skill.skill_id || "Skill" }))
        }
      ]
    : [];

  const messageStream = sharedDetail ? (
    <div className="message-stream">
      {sharedTimeline.map((messageCard) => {
        const isNotice = messageCard.role === "system";
        const messageComments = commentsByMessage.get(messageCard.messageId) || [];
        return (
          <div key={messageCard.messageId} className="shared-message-block">
            <MessageCard
              message={{ ...messageCard, disablePersonalWorkspaceFallback: true }}
              onNavigateCitation={onNavigateCitation}
            />
            {!isNotice ? (
              <button
                type="button"
                className="shared-message-comment-toggle"
                onClick={() => setActiveCommentTarget({ messageId: messageCard.messageId })}
              >
                评论 {messageComments.length}
              </button>
            ) : null}
          </div>
        );
      })}
      {activeTurnId && liveTurn.turnId === activeTurnId ? (
        <div className="shared-message-block" aria-live="polite">
          <MessageCard
            message={{
              messageId: `shared-live-${activeTurnId}`,
              turnId: activeTurnId,
              role: "assistant",
              time: "刚刚",
              status: liveTurn.assistantText ? "生成中" : "执行中",
              markdown: sanitizeSkillPromptLeak(liveTurn.assistantText),
              placeholder: liveTurn.activity || "正在组织回答，请稍候。",
              citations: liveTurn.citations,
              bullets: liveTurnPreviousActivities,
              sections: liveTurnSections,
              isStreaming: true,
              disablePersonalWorkspaceFallback: true
            }}
            onNavigateCitation={onNavigateCitation}
          />
        </div>
      ) : null}
      {turnInProgress || queuedFollowUps.length || failedFollowUps.length ? (
        <section className="shared-turn-state" aria-live="polite">
          {turnInProgress ? (
            <div className="shared-turn-running">
              <span className="shared-turn-pulse" aria-hidden="true" />
              <div>
                <strong>{runningTurnPresentation.title}</strong>
                <p>{runningTurnPresentation.description}</p>
              </div>
            </div>
          ) : null}
          {queuedFollowUps.length ? (
            <div className="shared-follow-up-queue" role="list" aria-label="排队中的追问">
              {queuedFollowUps.map((item, index) => (
                <article key={item.request_id} className="shared-follow-up-queued" role="listitem">
                  <span className="shared-follow-up-position">{index + 1}</span>
                  <div>
                    <strong>{authorName(item.requested_by)} · 排队中</strong>
                    <p>{item.content}</p>
                  </div>
                </article>
              ))}
            </div>
          ) : null}
          {failedFollowUps.length ? (
            <div className="shared-follow-up-failures" role="list" aria-label="失败的追问">
              {failedFollowUps.map((item) => (
                <article key={item.request_id} className="shared-follow-up-failed" role="listitem">
                  <div>
                    <strong>{authorName(item.requested_by)} · 追问失败</strong>
                    <p>
                      {item.requires_runtime_rebuild
                        ? "原 Agent Runtime 上下文已失效，需要确认重建后才能继续。"
                        : item.error_message || "Agent Runtime 执行失败。"}
                    </p>
                  </div>
                  {item.can_rebuild_runtime ? (
                    <button
                      type="button"
                      className="button button-secondary"
                      onClick={() => {
                        setRebuildError("");
                        setDismissedRebuildRequestIds((current) =>
                          current.filter((requestId) => requestId !== item.request_id)
                        );
                      }}
                    >
                      处理
                    </button>
                  ) : null}
                </article>
              ))}
            </div>
          ) : null}
        </section>
      ) : null}
      <section className="shared-follow-up-composer" aria-label="共同问答">
        <div className="shared-follow-up-heading">
          <p className={followUpLocked ? undefined : "shared-follow-up-participation-copy"}>
            {followUpLocked
              ? runtimeRebuildFailure
                ? "需要先确认是否重建 Agent Runtime，处理后即可继续。"
                : runningFollowUp
                  ? runningTurnPresentation.isOwnFollowUp
                    ? "你的追问正在处理中，完成后即可继续。"
                    : `${authorName(runningFollowUp.requested_by)} 正在追问，完成后即可继续。`
                  : "当前会话已有进行中或排队中的追问，完成后即可继续。"
              : "分享者和被分享者都可以在这个会话中提问并获得回答"}
          </p>
          {followUpLocked ? <span className="shared-follow-up-lock">暂不可发送</span> : null}
        </div>
        {currentUser ? (
          <div className="shared-follow-up-input-row">
            <textarea
              value={followUpDraft}
              placeholder={followUpLocked ? "等待当前回复完成…" : "输入你想问的问题"}
              disabled={followUpLocked || followUpSubmitting}
              onChange={(event) => setFollowUpDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  void handleFollowUpSubmit();
                }
              }}
            />
            <span
              className="shared-follow-up-submit-hint"
              title="每次只执行一条追问；同时发送时，后到的请求会自动排队。"
            >
              <button
                type="button"
                className="button button-primary"
                aria-label="发送追问；每次只执行一条追问，同时发送时后到的请求会自动排队"
                disabled={!followUpDraft.trim() || followUpLocked || followUpSubmitting}
                onClick={() => void handleFollowUpSubmit()}
              >
                {followUpSubmitting ? "发送中" : "发送追问"}
              </button>
            </span>
          </div>
        ) : (
          <button type="button" className="button button-secondary" onClick={onRequireLogin}>
            登录后参与问答
          </button>
        )}
        {followUpError ? <p className="form-error">{followUpError}</p> : null}
      </section>
    </div>
  ) : null;

  const commentDrawer = activeCommentTarget ? (
    <aside className="shared-comment-drawer" aria-label={commentDrawerTitle}>
      <div className="shared-comment-drawer-header">
        <div>
          <p className="assistant-history-eyebrow">{activeCommentTarget.messageId ? "Message Comments" : "Session Comments"}</p>
          <h2>{commentDrawerTitle}</h2>
        </div>
        <button type="button" className="icon-button" onClick={() => setActiveCommentTarget(null)} aria-label="关闭评论">
          ×
        </button>
      </div>
      {activeMessage ? <p className="shared-comment-drawer-context">{activeMessage.content}</p> : null}
      <CommentsBlock
        sessionId={sessionId}
        messageId={activeCommentTarget.messageId}
        comments={activeComments}
        currentUser={currentUser}
        onRefresh={refreshComments}
      />
    </aside>
  ) : null;

  if (!shareToken) {
    if (anonymous) {
      return (
        <div className="shared-public-page">
          <SharedPublicTopBar onRequireLogin={onRequireLogin} />
          <main className="shared-public-body">
            <div className="shared-public-empty">
              <p className="shared-public-empty-icon" aria-hidden="true">🔒</p>
              <h2>共享会话列表需要登录查看</h2>
              <p>登录后可查看向你公开的会话；也可以通过分享链接直接访问单条会话。</p>
              <button type="button" className="button button-primary" onClick={onRequireLogin}>
                去登录
              </button>
            </div>
          </main>
        </div>
      );
    }

    return (
      <div className="assistant-history-shell shared-shell">
        <header className="assistant-history-header">
          <div className="assistant-history-copy">
            <p className="assistant-history-eyebrow">Shared Sessions</p>
            <h1 className="chat-title">共享会话</h1>
            <p className="assistant-history-note">这里列出当前账号可查看的公开会话和指定给你的会话。</p>
          </div>
        </header>

        <section className="assistant-history-list">
          {loading ? <div className="folder-empty">正在加载共享会话...</div> : null}
          {!loading && error ? <div className="folder-empty">{error}</div> : null}
          {!loading && !error && sharedSessions.length === 0 ? <div className="folder-empty">暂时没有可见的共享会话。</div> : null}
          {!loading && !error
            ? sharedSessions.map((item) => (
                <button
                  key={item.share_id}
                  type="button"
                  className="history-session-card shared-session-card"
                  onClick={() => onNavigateShared?.(item.share_token)}
                >
                  <div className="history-session-main">
                    <strong>{item.session.title}</strong>
                    <div className="history-session-scope">
                      Git Scope · {formatGitScopeSummary(item.session.git_scopes || []) || "baseline"}
                    </div>
                    <p>{item.session.last_message_preview || "这条共享会话还没有内容摘要。"}</p>
                  </div>
                  <div className="history-session-meta">
                    <span>{getShareScopeLabel(shareScopeFromShare(item))}</span>
                    <span>{authorName(item.owner)}</span>
                    <span>{item.comment_count} 条评论</span>
                    <span>{formatTime(item.created_at)}</span>
                  </div>
                </button>
              ))
            : null}
        </section>
      </div>
    );
  }

  if (anonymous) {
    return (
      <div className="shared-public-page">
        <SharedPublicTopBar onRequireLogin={onRequireLogin} />
        <main className="shared-public-body">
          {loading ? <div className="folder-empty">正在加载共享内容...</div> : null}
          {!loading && error ? (
            <div className="shared-public-empty">
              <p className="shared-public-empty-icon" aria-hidden="true">⛓️</p>
              <h2>该共享链接不存在或已撤销</h2>
              <p>可能已被分享者撤销；也可能这是一条仅指定成员可见的分享，登录后或许可以访问。</p>
              <button type="button" className="button button-primary" onClick={onRequireLogin}>
                登录后查看
              </button>
            </div>
          ) : null}
          {!loading && sharedDetail ? (
            <div className="shared-public-detail">
              <header className="shared-public-header">
                <p className="assistant-history-eyebrow">Shared Session</p>
                <h1 className="shared-public-title">{sharedDetail.share?.session?.title || "共享会话"}</h1>
                <p className="shared-public-meta">
                  <span>{authorName(sharedDetail.share.owner)} 共享</span>
                  <span>{formatTime(sharedDetail.share.created_at)}</span>
                  <span>{sharedDetail.messages.length} 条消息</span>
                  <span>{sharedDetail.share.comment_count} 条评论</span>
                  <span>
                    Git Scope · {formatGitScopeSummary(sharedDetail.share.session.git_scopes || []) || "baseline"}
                  </span>
                </p>
              </header>
              {messageStream}
            </div>
          ) : null}
        </main>
        {commentDrawer}
      </div>
    );
  }

  return (
    <div className="assistant-shell shared-detail-shell">
      <header className="chat-header shared-detail-header">
        <div className="header-copy">
          <h1 className="chat-title">{sharedDetail?.share?.session?.title || "共享会话"}</h1>
          {sharedDetail ? (
            <div className="chat-title-meta">
              <span className="chat-title-meta-item">共享者：{authorName(sharedDetail.share.owner)}</span>
              <span className="chat-title-meta-item">范围：{getShareScopeLabel(shareScopeFromShare(sharedDetail.share))}</span>
              <span className="chat-title-meta-item">
                Git Scope · {formatGitScopeSummary(sharedDetail.share.session.git_scopes || []) || "baseline"}
              </span>
            </div>
          ) : null}
        </div>
        <div className="header-actions">
          <button type="button" className="button button-secondary" onClick={() => onNavigateShared?.("")}>
            返回列表
          </button>
          {sharedDetail ? (
            <button
              type="button"
              className="button button-secondary"
              onClick={() => setActiveCommentTarget({ messageId: null })}
            >
              评论 {sessionComments.length}
            </button>
          ) : null}
        </div>
      </header>

      <section className="chat-stage shared-detail-stage">
        {loading ? <div className="folder-empty">正在加载共享内容...</div> : null}
        {!loading && error ? <div className="folder-empty">{error}</div> : null}
        {!loading && sharedDetail ? <div className="shared-content-grid">{messageStream}</div> : null}
      </section>
      {commentDrawer}
      {rebuildPrompt ? (
        <RuntimeRebuildDialog
          request={rebuildPrompt}
          submitting={rebuildSubmitting}
          error={rebuildError}
          onCancel={() => {
            setRebuildError("");
            setDismissedRebuildRequestIds((current) => current.concat(rebuildPrompt.request_id));
          }}
          onConfirm={() => void handleRuntimeRebuild(rebuildPrompt)}
        />
      ) : null}
    </div>
  );
}
