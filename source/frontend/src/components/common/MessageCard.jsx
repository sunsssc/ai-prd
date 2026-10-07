import { useEffect, useMemo, useRef, useState } from "react";
import Card from "./Card";
import ActionIcon from "./ActionIcon";
import CopyIconButton from "./CopyIconButton";
import MarkdownRenderer from "./MarkdownRenderer";
import { getKnowledgeScopeByPath } from "../../data/knowledgeScopes";
import {
  buildPersonalWorkspaceFileUrl,
  normalizePersonalWorkspaceFilePath,
  resolvePersonalWorkspaceLinkHref
} from "../../utils/personalWorkspaceLinks";
import { resolveRequirementReferenceHref } from "../../utils/requirementReferences";

function normalizeLabel(label) {
  return label
    .replace(/^手动 @需求:\s*/, "")
    .replace(/^页面选择:\s*/, "")
    .replace(/^需求范围:\s*/, "")
    .replace(/^业务域:\s*/, "")
    .replace(/^代码范围:\s*/, "")
    .replace(/^需求文档:\s*/, "")
    .replace(/^业务文档:\s*/, "")
    .replace(/^关联业务文档:\s*/, "")
    .replace(/^关联代码:\s*/, "")
    .replace(/^关联需求:\s*/, "")
    .replace(/^引用文件:\s*/, "")
    .replace(/^需求文档入口:\s*/, "")
    .replace(/^业务文档入口:\s*/, "")
    .replace(/^代码范围入口:\s*/, "")
    .replace(/^补充需求:\s*/, "")
    .replace(/^可选代码范围:\s*/, "")
    .replace(/^待补需求文档:\s*/, "")
    .replace(/^待补业务文档:\s*/, "");
}

function buildSummary(items = [], limit = 2) {
  if (items.length === 0) {
    return "";
  }

  const labels = items.slice(0, limit).map((item) => normalizeLabel(item.label));
  const suffix = items.length > limit ? ` +${items.length - limit}` : "";
  return `${labels.join("、")}${suffix}`;
}

function SummaryBlock({ label, items, limit = 2, showLabel = true }) {
  if (!items?.length) {
    return null;
  }

  return (
    <div className={`summary-block ${showLabel ? "" : "summary-block-plain"}`.trim()}>
      <div className={`summary-row ${showLabel ? "" : "summary-row-plain"}`.trim()}>
        {showLabel ? <span className="summary-label">{label}</span> : null}
        <span className="summary-text">{buildSummary(items, limit)}</span>
      </div>
    </div>
  );
}

function buildMessageCopyText(message) {
  const segments = [];

  if (message.markdown?.trim()) {
    segments.push(message.markdown.trim());
  }

  if (!message.markdown && message.title?.trim()) {
    segments.push(message.title.trim());
  }

  if (!message.markdown && message.detail?.trim()) {
    segments.push(message.detail.trim());
  }

  if (!message.markdown && message.placeholder?.trim()) {
    segments.push(message.placeholder.trim());
  }

  if (message.bullets?.length) {
    segments.push(message.bullets.map((bullet) => `- ${bullet}`).join("\n"));
  }

  return segments.join("\n\n").trim();
}

function downloadButtonLabel(downloadState) {
  if (downloadState === "success") {
    return "已下载 Markdown";
  }

  if (downloadState === "error") {
    return "下载失败";
  }

  if (downloadState === "downloading") {
    return "正在下载";
  }

  return "下载 Markdown";
}

function MessageActions({
  visible,
  copyText,
  onDownload,
  downloadState,
  downloadAvailable,
  feedback,
  feedbackSaving,
  onFeedback,
  onVisualize,
  visualizeAvailable
}) {
  if (!visible) {
    return null;
  }

  const likeActive = feedback === "like";
  const dislikeActive = feedback === "dislike";

  return (
    <div className="message-actions" aria-label="回答操作">
      <CopyIconButton value={copyText} idleLabel="复制回答" />
      {onVisualize ? (
        <button
          type="button"
          className="message-action-button message-action-button-visualize"
          aria-label="可视化本轮问答"
          title="可视化本轮问答"
          disabled={!visualizeAvailable}
          onClick={onVisualize}
        >
          <ActionIcon kind="diagram" />
        </button>
      ) : null}
      {onFeedback ? (
        <>
          <button
            type="button"
            className={`message-action-button ${likeActive ? "message-action-button-active message-action-button-active-like" : ""}`.trim()}
            aria-label={likeActive ? "取消有帮助标记" : "标记有帮助"}
            aria-pressed={likeActive}
            title={likeActive ? "取消有帮助标记" : "标记有帮助"}
            disabled={feedbackSaving}
            onClick={() => onFeedback("like")}
          >
            <ActionIcon kind="thumb-up" />
          </button>
          <button
            type="button"
            className={`message-action-button ${dislikeActive ? "message-action-button-active message-action-button-active-dislike" : ""}`.trim()}
            aria-label={dislikeActive ? "取消没帮助标记" : "标记没帮助"}
            aria-pressed={dislikeActive}
            title={dislikeActive ? "取消没帮助标记" : "标记没帮助"}
            disabled={feedbackSaving}
            onClick={() => onFeedback("dislike")}
          >
            <ActionIcon kind="thumb-down" />
          </button>
        </>
      ) : null}
      <button
        type="button"
        className={`message-action-button ${downloadState !== "idle" ? `message-action-button-${downloadState}` : ""}`.trim()}
        aria-label={downloadButtonLabel(downloadState)}
        title={downloadButtonLabel(downloadState)}
        disabled={!downloadAvailable || downloadState === "downloading"}
        onClick={onDownload}
      >
        <ActionIcon kind="download" />
      </button>
    </div>
  );
}

function UserMessageActions({ copyText, onCopyToComposer }) {
  return (
    <div className="message-actions message-actions-user" aria-label="提问操作">
      <CopyIconButton value={copyText} idleLabel="复制提问" />
      {onCopyToComposer ? (
        <button
          type="button"
          className="message-action-button"
          aria-label="复制到输入框"
          title="复制文本和图片到输入框"
          onClick={onCopyToComposer}
        >
          <ActionIcon kind="copy-to-input" />
        </button>
      ) : null}
    </div>
  );
}

function TurnScopeMeta({ scopeLabel }) {
  if (!scopeLabel) {
    return null;
  }

  return (
    <div className="message-turn-scope" title={`本轮 Git Scope：${scopeLabel}`}>
      <span>Git Scope</span>
      <span aria-hidden="true">·</span>
      <span className="message-turn-scope-value">{scopeLabel}</span>
    </div>
  );
}

function resolvesToKnowledgeFile(path) {
  return Boolean(getKnowledgeScopeByPath(path));
}

function citationDisplayLabel(citation) {
  const raw = citation.label || citation.path || citation.citation_type || "";
  // 如果 label 是路径（绝对或相对），只取文件名
  if (raw.includes("/") || raw.includes("\\")) {
    return raw.replace(/\\/g, "/").split("/").filter(Boolean).pop() || raw;
  }
  return raw;
}

function buildFileArtifactMap(fileArtifacts = []) {
  const result = new Map();
  for (const artifact of fileArtifacts) {
    const displayPath = artifact.display_path || artifact.displayPath || "";
    const downloadUrl = artifact.download_url || artifact.downloadUrl || "";
    const normalized = normalizePersonalWorkspaceFilePath(displayPath);
    if (!normalized || !downloadUrl) {
      continue;
    }
    result.set(normalized, downloadUrl);
  }
  return result;
}

function resolveArtifactDownloadUrl(path, artifactMap) {
  const normalized = normalizePersonalWorkspaceFilePath(path);
  if (!normalized) {
    return "";
  }
  return artifactMap.get(normalized) || "";
}

function CitationLink({ citation, onNavigateCitation, artifactMap, disablePersonalWorkspaceFallback = false }) {
  const label = citationDisplayLabel(citation);
  const canNavigate = Boolean(onNavigateCitation && resolvesToKnowledgeFile(citation.path));
  const artifactDownloadUrl = resolveArtifactDownloadUrl(citation.path || "", artifactMap);
  const fallbackDownloadUrl = disablePersonalWorkspaceFallback ? "" : buildPersonalWorkspaceFileUrl(citation.path || "");
  const downloadUrl = artifactDownloadUrl || fallbackDownloadUrl;

  if (!canNavigate) {
    if (downloadUrl) {
      return (
        <a className="citation-link" href={downloadUrl}>
          {label}
        </a>
      );
    }
    return <span className="citation-label">{label}</span>;
  }

  return (
    <button
      type="button"
      className="citation-link"
      onClick={() => onNavigateCitation(citation)}
      title={citation.path}
    >
      {label}
    </button>
  );
}

function CitationBlock({ citations, onNavigateCitation, artifactMap, disablePersonalWorkspaceFallback = false }) {
  if (!citations?.length) {
    return null;
  }

  const displayed = citations.slice(0, 3);
  const overflow = citations.length - displayed.length;

  return (
    <div className="summary-block">
      <div className="summary-row">
        <span className="summary-label">来源</span>
        <span className="citation-list">
          {displayed.map((citation, index) => (
            <span key={citation.citation_id || index} className="citation-item">
              {index > 0 ? <span className="citation-sep">、</span> : null}
              <CitationLink
                citation={citation}
                onNavigateCitation={onNavigateCitation}
                artifactMap={artifactMap}
                disablePersonalWorkspaceFallback={disablePersonalWorkspaceFallback}
              />
            </span>
          ))}
          {overflow > 0 ? <span className="citation-overflow"> +{overflow}</span> : null}
        </span>
      </div>
    </div>
  );
}

function uploadedImageUrl(imageId) {
  return `/api/assistant/uploads/images/${encodeURIComponent(imageId)}`;
}

function getMessageImages(message) {
  return (message.sources || [])
    .filter((source) => source.sourceType === "image" && source.imageId)
    .map((source) => ({
      ...source,
      url: source.imageUrl || uploadedImageUrl(source.imageId)
    }));
}

function MessageImageBlock({ images }) {
  if (!images.length) {
    return null;
  }

  return (
    <div className="message-image-grid" aria-label="消息图片">
      {images.map((image) => (
        <a
          key={image.imageId}
          className="message-image-link"
          href={image.url}
          target="_blank"
          rel="noreferrer"
          title={image.label || "查看图片"}
        >
          <img src={image.url} alt={image.label || "上传图片"} loading="lazy" />
        </a>
      ))}
    </div>
  );
}

function ProgressCopy({ children, spinning = false }) {
  return (
    <p className={`message-copy ${spinning ? "message-copy-progress" : ""}`.trim()}>
      <span>{children}</span>
      {spinning ? <span className="message-progress-spinner" aria-hidden="true" /> : null}
    </p>
  );
}

export default function MessageCard({
  message,
  onNavigateCitation,
  onFeedback,
  onVisualize,
  onCopyToComposer,
  requirementReferences = null
}) {
  const [downloadState, setDownloadState] = useState("idle");
  const [feedbackSaving, setFeedbackSaving] = useState(false);
  const [activeView, setActiveView] = useState("text");
  const visualizationKeyRef = useRef("");
  const copyText = buildMessageCopyText(message);
  const canDownload = Boolean(message.markdown?.trim());
  const messageImages = getMessageImages(message);
  const hasVisualization = Boolean(message.visualization);
  const showingVisualization = hasVisualization && activeView === "visual";
  const visualizationRunning = Boolean(message.visualization?.isStreaming);
  const placeholderRunning = Boolean(message.role === "assistant" && message.isStreaming);
  const artifactMap = useMemo(() => buildFileArtifactMap(message.fileArtifacts), [message.fileArtifacts]);
  const disablePersonalWorkspaceFallback = Boolean(message.disablePersonalWorkspaceFallback);
  const resolveMessageAssetHref = useMemo(
    () => (href = "") => {
      const artifactDownloadUrl = resolveArtifactDownloadUrl(href, artifactMap);
      if (artifactDownloadUrl) {
        return artifactDownloadUrl;
      }
      if (disablePersonalWorkspaceFallback && normalizePersonalWorkspaceFilePath(href)) {
        return "";
      }
      return resolvePersonalWorkspaceLinkHref(href);
    },
    [artifactMap, disablePersonalWorkspaceFallback]
  );
  const resolveMessageLinkHref = useMemo(
    () => (href = "") =>
      resolveRequirementReferenceHref(href, requirementReferences) || resolveMessageAssetHref(href),
    [requirementReferences, resolveMessageAssetHref]
  );

  useEffect(() => {
    if (!hasVisualization) {
      visualizationKeyRef.current = "";
      setActiveView("text");
      return;
    }

    const visualizationKey = `${message.messageId}:${message.visualization?.requestTurnId || ""}`;
    if (visualizationKeyRef.current !== visualizationKey) {
      visualizationKeyRef.current = visualizationKey;
      setActiveView("visual");
    }
  }, [hasVisualization, message.messageId, message.visualization?.requestTurnId]);

  useEffect(() => {
    if (downloadState === "idle") {
      return undefined;
    }

    const timerId = window.setTimeout(() => {
      setDownloadState("idle");
    }, 2000);

    return () => {
      window.clearTimeout(timerId);
    };
  }, [downloadState]);

  function buildDownloadName() {
    const stamp = new Date()
      .toISOString()
      .replace(/[:]/g, "-")
      .replace(/\.\d{3}Z$/, "Z");
    return `ai-prd-answer-${stamp}.md`;
  }

  function handleDownload() {
    if (!canDownload || downloadState === "downloading") {
      return;
    }

    setDownloadState("downloading");

    try {
      const blob = new Blob([message.markdown.trim()], { type: "text/markdown;charset=utf-8" });
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = buildDownloadName();
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(objectUrl);
      setDownloadState("success");
    } catch {
      setDownloadState("error");
    }
  }

  async function handleFeedback(feedback) {
    if (!onFeedback || feedbackSaving || !message.messageId) {
      return;
    }

    setFeedbackSaving(true);
    try {
      await onFeedback(message.messageId, feedback);
    } finally {
      setFeedbackSaving(false);
    }
  }

  const avatarLabel = message.role === "assistant" ? "AI" : message.role === "system" ? "系统" : "你";

  return (
    <div className={`message-row message-row-${message.role}`}>
      <div className="message-avatar">{avatarLabel}</div>
      <Card className={`message-card message-card-${message.role}`}>
        <div className="message-meta">
          <span>{message.time}</span>
          {message.status ? <span>{message.status}</span> : null}
          {message.scopeLabel && message.role !== "assistant" ? (
            <span className="message-scope-label" title={`本轮 Git Scope：${message.scopeLabel}`}>
              Git Scope · {message.scopeLabel}
            </span>
          ) : null}
        </div>

        {message.sectionLabel && !message.sources?.length ? <div className="section-kicker">{message.sectionLabel}</div> : null}

        {message.sources?.length ? (
          <SummaryBlock
            label={message.sectionLabel || "本轮新增指定"}
            items={message.sources}
            showLabel={message.role !== "user"}
          />
        ) : null}

        {message.title ? <h2 className="message-title">{message.title}</h2> : null}
        <MessageImageBlock images={messageImages} />
        {hasVisualization ? (
          <div className="message-view-tabs" role="tablist" aria-label="回答视图">
            <button
              type="button"
              className={`message-view-tab ${activeView === "text" ? "message-view-tab-active" : ""}`.trim()}
              aria-pressed={activeView === "text"}
              onClick={() => setActiveView("text")}
            >
              文本
            </button>
            <button
              type="button"
              className={`message-view-tab ${activeView === "visual" ? "message-view-tab-active" : ""} ${
                visualizationRunning ? "message-view-tab-progress" : ""
              }`.trim()}
              aria-pressed={activeView === "visual"}
              onClick={() => setActiveView("visual")}
            >
              {visualizationRunning ? (
                <span className="processing-text">
                  <span className="processing-text-highlight">可视化</span>
                </span>
              ) : (
                "可视化"
              )}
            </button>
          </div>
        ) : null}
        {!showingVisualization && message.markdown ? (
          <MarkdownRenderer
            markdown={message.markdown}
            streaming={message.isStreaming}
            resolveImageSrc={resolveMessageAssetHref}
            resolveLinkHref={resolveMessageLinkHref}
            requirementReferences={message.role === "assistant" ? requirementReferences : null}
          />
        ) : null}
        {showingVisualization && message.visualization?.markdown ? (
          <MarkdownRenderer
            markdown={message.visualization.markdown}
            streaming={message.visualization.isStreaming}
            resolveImageSrc={resolveMessageAssetHref}
            resolveLinkHref={resolveMessageLinkHref}
            requirementReferences={message.role === "assistant" ? requirementReferences : null}
          />
        ) : null}
        {showingVisualization && !message.visualization?.markdown && message.visualization?.placeholder ? (
          <ProgressCopy spinning={visualizationRunning}>{message.visualization.placeholder}</ProgressCopy>
        ) : null}
        {showingVisualization && visualizationRunning && message.visualization?.activities?.length ? (
          <ul className="message-list message-visualization-progress">
            {message.visualization.activities.map((activity, index) => (
              <li key={`${index}-${activity}`}>{activity}</li>
            ))}
          </ul>
        ) : null}
        {!showingVisualization && !message.markdown && message.detail ? <ProgressCopy>{message.detail}</ProgressCopy> : null}
        {!showingVisualization && !message.markdown && message.placeholder ? (
          <ProgressCopy spinning={placeholderRunning}>{message.placeholder}</ProgressCopy>
        ) : null}

        {!showingVisualization && message.bullets?.length ? (
          <ul className="message-list">
            {message.bullets.map((bullet) => (
              <li key={bullet}>{bullet}</li>
            ))}
          </ul>
        ) : null}

        {!showingVisualization && message.sections?.length ? (
          <div className="summary-stack">
            {message.sections.map((group) => (
              <SummaryBlock
                key={group.label}
                label={group.label}
                items={group.sources}
                limit={2}
              />
            ))}
          </div>
        ) : null}

        {!showingVisualization && message.citations?.length ? (
          <CitationBlock
            citations={message.citations}
            onNavigateCitation={onNavigateCitation}
            artifactMap={artifactMap}
            disablePersonalWorkspaceFallback={disablePersonalWorkspaceFallback}
          />
        ) : null}

        {message.role === "assistant" ? <TurnScopeMeta scopeLabel={message.scopeLabel} /> : null}

        <MessageActions
          visible={message.role === "assistant" && message.status === "回复"}
          copyText={copyText}
          onDownload={handleDownload}
          downloadState={downloadState}
          downloadAvailable={canDownload}
          feedback={message.feedback}
          feedbackSaving={feedbackSaving}
          onFeedback={onFeedback ? handleFeedback : undefined}
          onVisualize={onVisualize}
          visualizeAvailable={Boolean(onVisualize && message.markdown?.trim())}
        />
        {message.role === "user" ? (
          <UserMessageActions copyText={copyText} onCopyToComposer={onCopyToComposer} />
        ) : null}
      </Card>
    </div>
  );
}
