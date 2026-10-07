import { sanitizeSkillPromptLeak } from "./assistantVisualizations.js";

export function formatSharedMemberNames(members = []) {
  const names = members.map((member) => member?.name || member?.email || "未知用户");
  const visibleNames = names.slice(0, 2).join("、");
  return names.length > 2 ? `${visibleNames}…` : visibleNames;
}

export function shouldAppendStreamMessage({
  view,
  routeSessionId,
  streamSessionId
}) {
  if (view !== "chat") {
    return false;
  }
  return (routeSessionId || null) === (streamSessionId || null);
}

export function buildStreamFailedTurnTrace({
  sessionId,
  turnId,
  errorMessage,
  gitScopes = [],
  completedAt = new Date().toISOString()
}) {
  if (!sessionId || !turnId) {
    return null;
  }

  return {
    turn: {
      turn_id: turnId,
      session_id: sessionId,
      assistant_message_id: null,
      completed_at: completedAt,
      status: "failed",
      error_message: errorMessage || "执行过程中出现错误。"
    },
    runtime_events: [],
    context_requests: [],
    git_scopes: gitScopes
  };
}

export function buildUserMessageSourcesFromContextRequests(contextRequests = [], { imageUrlForId } = {}) {
  return contextRequests.map((item) => {
    const imageId =
      item.source_type === "image" ? item.metadata?.image_id || item.context_key?.replace(/^uploaded-image:/, "") : undefined;
    return {
      label: item.label,
      sourceUri: item.source_uri,
      scopeId: item.metadata?.scope,
      sourceType: item.source_type,
      imageId,
      imageUrl: imageId && imageUrlForId ? imageUrlForId(imageId) : undefined,
      contextItem: item
    };
  });
}

export function buildComposerDraftFromUserMessage(message, { imageUrlForId } = {}) {
  if (message?.role !== "user") {
    return null;
  }

  const images = (message.sources || [])
    .filter((source) => source.sourceType === "image" && source.imageId)
    .map((source) => ({
      id: source.imageId,
      label: source.label || "图片",
      fileName: source.fileName || source.contextItem?.metadata?.original_filename || source.label || "图片",
      objectUrl: source.imageUrl || imageUrlForId?.(source.imageId) || "",
      sourceUri: source.sourceUri,
      mimeType: source.mimeType || source.contextItem?.metadata?.mime_type,
      sizeBytes: source.sizeBytes || source.contextItem?.metadata?.size_bytes,
      sourceType: "image",
      contextItem:
        source.contextItem || {
          context_key: `uploaded-image:${source.imageId}`,
          label: source.label || "图片",
          content: null,
          source_type: "image",
          source_uri: source.sourceUri,
          metadata: { image_id: source.imageId }
        }
    }));

  return {
    content: message.title || "",
    images
  };
}

export function buildUserMessageSectionLabelFromContextRequests(contextRequests = []) {
  const hasScope = contextRequests.some((item) => item.source_type === "knowledge_scope");
  const hasReference = contextRequests.some((item) => item.source_type !== "knowledge_scope");
  if (hasScope && hasReference) {
    return "本轮知识范围与对象";
  }
  if (hasScope) {
    return "本轮知识范围";
  }
  if (hasReference) {
    return "本轮新增指定";
  }
  return undefined;
}

export function formatGitScopeSummary(gitScopes = []) {
  if (!gitScopes.length) {
    return "baseline";
  }
  return gitScopes
    .map((scope) => {
      const repo = String(scope.repo_full_name || "").split("/").pop() || "代码";
      const selector =
        scope.selector_type === "pull_request"
          ? `PR #${scope.selector_value}`
          : scope.selector_type === "branch"
            ? scope.selector_value
            : "";
      const revision = scope.resolved_sha ? String(scope.resolved_sha).slice(0, 7) : "";
      return [repo, selector, scope.strategy, revision].filter(Boolean).join(" · ");
    })
    .join(" / ");
}

export function groupTestDataPlansByTurn(plans = [], timeline = []) {
  const byTurn = new Map();
  for (const plan of plans) {
    const turnId = plan?.prepared_turn_id || "";
    byTurn.set(turnId, (byTurn.get(turnId) || []).concat(plan));
  }

  const assistantTurnIds = new Set(
    timeline
      .filter((message) => message?.role === "assistant")
      .map((message) => message.turnId)
      .filter(Boolean)
  );
  return {
    byTurn,
    orphaned: plans.filter((plan) => !assistantTurnIds.has(plan?.prepared_turn_id))
  };
}

export function toMessageCard(message, { formatTime, imageUrlForId } = {}) {
  const format = formatTime || ((value) => value || "");
  if (message.role === "assistant") {
    return {
      messageId: message.message_id,
      turnId: message.turn_id || null,
      role: "assistant",
      time: format(message.created_at),
      status: "回复",
      markdown: sanitizeSkillPromptLeak(message.content),
      citations: message.citations || [],
      fileArtifacts: message.file_artifacts || message.fileArtifacts || [],
      feedback: message.feedback || null,
      scopeLabel: formatGitScopeSummary(message.git_scopes || [])
    };
  }

  const contextRequests = message.context_requests || [];
  const sources = message.sources || buildUserMessageSourcesFromContextRequests(contextRequests, { imageUrlForId });

  return {
    messageId: message.message_id,
    turnId: message.turn_id || null,
    role: "user",
    time: format(message.created_at),
    status: message.author ? `${message.author.name || message.author.email || "共享成员"} · 追问` : "提问",
    title: message.content,
    sectionLabel: message.sectionLabel || buildUserMessageSectionLabelFromContextRequests(contextRequests),
    sources,
    scopeLabel: formatGitScopeSummary(message.git_scopes || [])
  };
}

export function toTimelineNoticeCard(notice, { formatTime } = {}) {
  const format = formatTime || ((value) => value || "");
  const resetDate = notice.resets_at ? new Date(notice.resets_at) : null;
  const resetText =
    resetDate && !Number.isNaN(resetDate.getTime())
      ? resetDate.toLocaleString("zh-CN", {
          month: "numeric",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit"
        })
      : "";
  const isGitContextUpdate = notice.kind === "git_context_updated";
  const message = String(
    notice.message || (isGitContextUpdate ? "代码上下文已更新。" : "本轮生图受到额度限制。")
  ).trim();
  return {
    messageId: `timeline-notice-${notice.event_id}`,
    turnId: notice.turn_id || null,
    role: "system",
    time: format(notice.created_at),
    status: "系统提示",
    title: isGitContextUpdate ? "代码版本更新" : "生图额度",
    detail: resetText ? `${message} ${resetText} 重置。` : message
  };
}

export function attachTimelineNotices(messageCards, notices, { formatTime } = {}) {
  if (!notices?.length) {
    return messageCards;
  }
  const noticesByTurn = new Map();
  for (const notice of notices) {
    const turnId = notice.turn_id || "";
    const grouped = noticesByTurn.get(turnId) || [];
    grouped.push(notice);
    noticesByTurn.set(turnId, grouped);
  }

  const result = [];
  const attachedEventIds = new Set();
  for (const card of messageCards) {
    result.push(card);
    if (card.role !== "assistant" || !card.turnId) {
      continue;
    }
    for (const notice of noticesByTurn.get(card.turnId) || []) {
      result.push(toTimelineNoticeCard(notice, { formatTime }));
      attachedEventIds.add(notice.event_id);
    }
  }
  for (const notice of notices) {
    if (!attachedEventIds.has(notice.event_id)) {
      result.push(toTimelineNoticeCard(notice, { formatTime }));
    }
  }
  return result;
}
