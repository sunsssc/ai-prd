import { Fragment, memo, useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import remarkGfm from "remark-gfm";
import { encode } from "plantuml-encoder";
import ActionIcon from "./ActionIcon";
import CopyIconButton from "./CopyIconButton";
import { splitDocumentComments } from "../../utils/documentComments";
import { buildHtmlPreviewDocument, looksLikeHtmlArtifact, normalizeSvgArtifactSource } from "../../utils/htmlArtifacts";
import { remarkRequirementReferences } from "../../utils/requirementReferences";

const DIAGRAM_LANGUAGES = new Set(["mermaid", "plantuml", "puml"]);
const rehypePlugins = [rehypeRaw];
const baseRemarkPlugins = [remarkGfm];
const visualizerColor = {
  backgroundPrimary: "#fffdf8",
  backgroundSecondary: "#f8f3eb",
  borderSecondary: "#d8cbb8",
  purple50: "#EEEDFE",
  purple100: "#CECBF6",
  purple600: "#534AB7",
  purple800: "#3C3489",
  teal50: "#E1F5EE",
  teal100: "#9FE1CB",
  teal600: "#0F6E56",
  teal800: "#085041",
  coral50: "#FAECE7",
  coral100: "#F5C4B3",
  coral600: "#993C1D",
  coral800: "#712B13",
  pink50: "#FBEAF0",
  pink600: "#993556",
  pink800: "#72243E",
  blue50: "#E6F1FB",
  blue600: "#185FA5",
  blue800: "#0C447C",
  green50: "#EAF3DE",
  green600: "#3B6D11",
  amber50: "#FAEEDA",
  amber600: "#854F0B",
  red50: "#FCEBEB",
  red600: "#A32D2D",
  red800: "#791F1F",
  gray50: "#F1EFE8",
  gray100: "#D3D1C7",
  gray600: "#5F5E5A",
  gray800: "#444441",
  gray900: "#2C2C2A"
};
const mermaidVisualizerConfig = {
  startOnLoad: false,
  securityLevel: "antiscript",
  theme: "base",
  look: "classic",
  fontFamily: 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
  themeVariables: {
    background: visualizerColor.backgroundPrimary,
    primaryColor: visualizerColor.purple50,
    primaryTextColor: visualizerColor.purple800,
    primaryBorderColor: visualizerColor.purple600,
    secondaryColor: visualizerColor.teal50,
    secondaryTextColor: visualizerColor.teal800,
    secondaryBorderColor: visualizerColor.teal600,
    tertiaryColor: visualizerColor.coral50,
    tertiaryTextColor: visualizerColor.coral800,
    tertiaryBorderColor: visualizerColor.coral600,
    noteBkgColor: visualizerColor.pink50,
    noteTextColor: visualizerColor.pink800,
    noteBorderColor: visualizerColor.pink600,
    lineColor: visualizerColor.gray600,
    arrowheadColor: visualizerColor.gray600,
    textColor: visualizerColor.gray900,
    titleColor: visualizerColor.gray900,
    edgeLabelBackground: visualizerColor.backgroundPrimary,
    nodeBkg: visualizerColor.purple50,
    mainBkg: visualizerColor.purple50,
    nodeBorder: visualizerColor.purple600,
    nodeTextColor: visualizerColor.purple800,
    clusterBkg: visualizerColor.backgroundSecondary,
    clusterBorder: visualizerColor.borderSecondary,
    defaultLinkColor: visualizerColor.gray600,
    actorBkg: visualizerColor.purple50,
    actorBorder: visualizerColor.purple600,
    actorTextColor: visualizerColor.purple800,
    actorLineColor: visualizerColor.gray600,
    labelBoxBkgColor: visualizerColor.backgroundSecondary,
    labelBoxBorderColor: visualizerColor.borderSecondary,
    labelTextColor: visualizerColor.gray900,
    signalColor: visualizerColor.gray600,
    signalTextColor: visualizerColor.gray900,
    loopTextColor: visualizerColor.purple800,
    activationBkgColor: visualizerColor.teal50,
    activationBorderColor: visualizerColor.teal600,
    sequenceNumberColor: visualizerColor.coral600,
    sectionBkgColor: visualizerColor.teal50,
    altSectionBkgColor: visualizerColor.purple50,
    sectionBkgColor2: visualizerColor.coral50,
    taskBkgColor: visualizerColor.purple50,
    taskBorderColor: visualizerColor.purple600,
    activeTaskBkgColor: visualizerColor.teal50,
    activeTaskBorderColor: visualizerColor.teal600,
    doneTaskBkgColor: visualizerColor.green50,
    doneTaskBorderColor: visualizerColor.green600,
    critBkgColor: visualizerColor.red50,
    critBorderColor: visualizerColor.red600,
    taskTextColor: visualizerColor.gray900,
    taskTextOutsideColor: visualizerColor.gray600,
    gridColor: visualizerColor.gray100,
    todayLineColor: visualizerColor.coral600,
    stateBkg: visualizerColor.purple50,
    stateLabelColor: visualizerColor.purple800,
    transitionColor: visualizerColor.gray600,
    transitionLabelColor: visualizerColor.gray900,
    labelBackgroundColor: visualizerColor.backgroundPrimary,
    compositeBackground: visualizerColor.backgroundSecondary,
    compositeTitleBackground: visualizerColor.purple50,
    compositeBorder: visualizerColor.borderSecondary,
    altBackground: visualizerColor.teal50,
    innerEndBackground: visualizerColor.gray100,
    specialStateColor: visualizerColor.coral50,
    errorBkgColor: visualizerColor.red50,
    errorTextColor: visualizerColor.red800,
    personBkg: visualizerColor.purple50,
    personBorder: visualizerColor.purple600,
    rowOdd: visualizerColor.backgroundPrimary,
    rowEven: visualizerColor.backgroundSecondary,
    classText: visualizerColor.gray900,
    cScale0: visualizerColor.purple50,
    cScale1: visualizerColor.teal50,
    cScale2: visualizerColor.coral50,
    cScale3: visualizerColor.pink50,
    cScale4: visualizerColor.blue50,
    cScale5: visualizerColor.green50,
    cScale6: visualizerColor.amber50,
    cScale7: visualizerColor.red50,
    cScale8: visualizerColor.gray50,
    cScale9: visualizerColor.purple100,
    cScale10: visualizerColor.teal100,
    cScale11: visualizerColor.coral100,
    scaleLabelColor: visualizerColor.gray900,
    fillType0: visualizerColor.purple50,
    fillType1: visualizerColor.teal50,
    fillType2: visualizerColor.coral50,
    fillType3: visualizerColor.pink50,
    fillType4: visualizerColor.blue50,
    fillType5: visualizerColor.green50,
    fillType6: visualizerColor.amber50,
    fillType7: visualizerColor.red50,
    pie1: visualizerColor.purple50,
    pie2: visualizerColor.teal50,
    pie3: visualizerColor.coral50,
    pie4: visualizerColor.pink50,
    pie5: visualizerColor.blue50,
    pie6: visualizerColor.green50,
    pie7: visualizerColor.amber50,
    pie8: visualizerColor.red50,
    pieTitleTextColor: visualizerColor.gray900,
    pieSectionTextColor: visualizerColor.gray900,
    pieLegendTextColor: visualizerColor.gray900,
    pieStrokeColor: visualizerColor.backgroundPrimary,
    pieOuterStrokeColor: visualizerColor.borderSecondary,
    git0: visualizerColor.purple50,
    git1: visualizerColor.teal50,
    git2: visualizerColor.coral50,
    git3: visualizerColor.pink50,
    git4: visualizerColor.blue50,
    git5: visualizerColor.green50,
    git6: visualizerColor.amber50,
    git7: visualizerColor.red50,
    branchLabelColor: visualizerColor.gray900,
    tagLabelColor: visualizerColor.gray900,
    tagLabelBackground: visualizerColor.coral50,
    tagLabelBorder: visualizerColor.coral600,
    commitLabelColor: visualizerColor.gray900,
    commitLabelBackground: visualizerColor.backgroundPrimary,
    requirementBackground: visualizerColor.purple50,
    requirementBorderColor: visualizerColor.purple600,
    requirementTextColor: visualizerColor.purple800,
    relationColor: visualizerColor.gray600,
    relationLabelBackground: visualizerColor.backgroundPrimary,
    relationLabelColor: visualizerColor.gray900,
    fontFamily: 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    fontSize: "14px",
    fontWeight: "400",
    noteFontWeight: "400",
    useGradient: false,
    dropShadow: "none",
    radius: 4,
    strokeWidth: 1
  }
};

let mermaidModulePromise = null;

function loadMermaid() {
  if (!mermaidModulePromise) {
    mermaidModulePromise = import("mermaid").then((module) => {
      module.default.initialize(mermaidVisualizerConfig);
      return module.default;
    });
  }

  return mermaidModulePromise;
}

function isExternalHref(href = "") {
  return /^https?:\/\//i.test(href);
}

function looksLikePathSnippet(value = "") {
  const normalized = value.trim();
  if (!normalized || normalized.includes("\n")) {
    return false;
  }

  return /(?:^|\/)[^/\s]+\.[a-z0-9]+$/i.test(normalized) || normalized.startsWith("workspace/") || normalized.startsWith("source/");
}

function isBlockCodeNode(node, className = "", rawText = "") {
  if (node?.tagName === "code" && node?.properties && Object.hasOwn(node.properties, "className")) {
    return true;
  }

  if (typeof className === "string" && className.includes("language-")) {
    return true;
  }

  return rawText.includes("\n");
}

function flattenMarkdownChildren(children) {
  if (children === null || children === undefined || typeof children === "boolean") {
    return "";
  }
  if (typeof children === "string" || typeof children === "number") {
    return String(children);
  }
  if (Array.isArray(children)) {
    return children.map(flattenMarkdownChildren).join("");
  }
  if (typeof children === "object" && children.props) {
    return flattenMarkdownChildren(children.props.children);
  }
  return "";
}

function looksLikeReviewFindingTitle(children) {
  const text = flattenMarkdownChildren(children).trim();
  return /^(?:\[(?:高|中|低|P[0-3])\]|P[0-3]\s*[—-]|[高中低]风险\b)/.test(text);
}

function parseDocumentMetadataSection(markdown = "") {
  const metadataPattern = /^## (页面元信息|任务元信息)\s*\n+((?:- .*(?:\n|$))+)\n*/;

  const match = markdown.match(metadataPattern);

  if (!match) {
    return {
      content: markdown,
      metadataItems: [],
      metadataLabel: ""
    };
  }

  const metadataLabel = match[1];
  const metadataItems = match[2]
    .trim()
    .split("\n")
    .map((line) => line.replace(/^- /, "").trim())
    .map((line) => {
      const separatorIndex = line.indexOf(":");
      if (separatorIndex < 0) {
        return null;
      }

      const label = line.slice(0, separatorIndex).trim();
      const rawValue = line.slice(separatorIndex + 1).trim();
      const value = rawValue.replace(/^`(.+)`$/, "$1");

      if (!label || !value) {
        return null;
      }

      return {
        label,
        value,
        isLink: /^https?:\/\//i.test(value)
      };
    })
    .filter(Boolean);

  let content = markdown.slice(match[0].length);
  if (metadataLabel === "页面元信息") {
    content = content.replace(/^## 内容\s*\n*/, "");
  }

  return {
    content,
    metadataItems,
    metadataLabel
  };
}

function splitMarkdownTableCells(row = "") {
  const cells = [];
  let current = "";
  let escaped = false;
  const normalized = row.trim().replace(/^\|/, "").replace(/\|$/, "");

  for (const char of normalized) {
    if (char === "|" && !escaped) {
      cells.push(current.trim().replace(/\\\|/g, "|"));
      current = "";
      escaped = false;
      continue;
    }
    current += char;
    escaped = char === "\\" && !escaped;
  }

  cells.push(current.trim().replace(/\\\|/g, "|"));
  return cells;
}

function parseCustomFieldsSection(markdown = "") {
  const customFieldsPattern = /^## 自定义字段\s*\n+\| 字段 \| 值 \|\n\| --- \| --- \|\n((?:\|.*\|\n?)*)/m;
  const match = markdown.match(customFieldsPattern);

  if (!match) {
    return [{ type: "markdown", content: markdown }];
  }

  const before = markdown.slice(0, match.index);
  const after = markdown.slice((match.index || 0) + match[0].length).replace(/^\n+/, "");
  const metadataItems = match[1]
    .trim()
    .split("\n")
    .map((row) => splitMarkdownTableCells(row))
    .map(([label, value]) => {
      if (!label || !value) {
        return null;
      }
      return {
        label,
        value,
        isLink: /^https?:\/\//i.test(value)
      };
    })
    .filter(Boolean);

  const sections = [];
  if (before.trim()) {
    sections.push({ type: "markdown", content: before.trim() });
  }
  if (metadataItems.length) {
    sections.push({ type: "metadata", label: "自定义字段", items: metadataItems });
  }
  if (after.trim()) {
    sections.push(...parseCustomFieldsSection(after.trim()));
  }
  return sections;
}

function MetadataSection({ label, items, resolveLinkHref = null }) {
  const [expanded, setExpanded] = useState(false);

  if (!items?.length) {
    return null;
  }

  const linkItems = items.filter((item) => item.isLink);
  const detailItems = items.filter((item) => !item.isLink);
  const sectionLabel = label || "元信息";

  return (
    <section className="markdown-doc-meta markdown-doc-meta-collapsible" aria-label={sectionLabel}>
      <div className="markdown-doc-meta-summary">
        <button
          type="button"
          className="markdown-doc-meta-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((current) => !current)}
        >
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <path d="m5.5 3.5 4.5 4.5-4.5 4.5" />
          </svg>
          <span>{sectionLabel}</span>
        </button>
        {linkItems.length ? (
          <div className="markdown-doc-meta-summary-links">
            {linkItems.map((item) => {
              const resolvedHref = typeof resolveLinkHref === "function" ? resolveLinkHref(item.value) : item.value;
              return (
                <span key={`${item.label}-${item.value}`} className="markdown-doc-meta-link-row">
                  <a className="markdown-link markdown-doc-meta-link" href={resolvedHref} target="_blank" rel="noreferrer">
                    {item.value}
                  </a>
                  <CopyIconButton
                    value={item.value}
                    idleLabel="复制链接"
                    successLabel="链接已复制"
                    className="markdown-doc-meta-copy"
                  />
                </span>
              );
            })}
          </div>
        ) : null}
      </div>
      {expanded ? (
        <div className="markdown-doc-meta-details">
          {detailItems.map((item) => (
            <div key={`${item.label}-${item.value}`} className="markdown-doc-meta-item">
              <span className="markdown-doc-meta-label">{item.label}</span>
              <div className="markdown-doc-meta-value">
                <span className="markdown-doc-meta-text">{item.value}</span>
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function CommentPanelIcon({ collapsed = false }) {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <path d="M4.25 4.5h11.5v8H9l-3.5 3v-3H4.25z" />
      <path d={collapsed ? "m8.5 7.25 2.5 2.5-2.5 2.5" : "m11.5 7.25-2.5 2.5 2.5 2.5"} />
    </svg>
  );
}

function formatCommentTime(value = "") {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }).format(date);
}

function countComments(comments = []) {
  return comments.reduce((total, comment) => total + 1 + countComments(comment.replies || []), 0);
}

function CommentCard({ comment, markdownComponents, isReply = false, onReply }) {
  const author = comment.author || "评论";
  return (
    <article className={`markdown-comment ${isReply ? "markdown-comment-reply" : ""}`.trim()}>
      <div className="markdown-comment-avatar" aria-hidden="true">
        {author.slice(0, 1).toUpperCase()}
      </div>
      <div className="markdown-comment-main">
        <div className="markdown-comment-meta">
          <strong>{author}</strong>
          {comment.createdAt ? <span>{formatCommentTime(comment.createdAt)}</span> : null}
        </div>
        {comment.content ? (
          <div className="markdown-comment-content">
            <ReactMarkdown
              rehypePlugins={rehypePlugins}
              remarkPlugins={baseRemarkPlugins}
              components={markdownComponents}
            >
              {comment.content}
            </ReactMarkdown>
          </div>
        ) : null}
        {!isReply && onReply && comment.commentId ? (
          <button type="button" className="markdown-comment-reply-button" onClick={() => onReply(comment.commentId)}>
            回复
          </button>
        ) : null}
      </div>
    </article>
  );
}

function CommentSection({
  comments,
  markdownComponents,
  collapsed,
  onToggleCollapsed,
  loading = false,
  error = "",
  onCreate = null,
  onReply = null
}) {
  const [newComment, setNewComment] = useState("");
  const [replyTarget, setReplyTarget] = useState("");
  const [replyContent, setReplyContent] = useState("");
  const [submitting, setSubmitting] = useState("");
  const [submitError, setSubmitError] = useState("");
  const totalCount = countComments(comments);

  async function submitComment(event) {
    event.preventDefault();
    const content = newComment.trim();
    if (!content || !onCreate) return;
    setSubmitting("new");
    setSubmitError("");
    try {
      await onCreate(content);
      setNewComment("");
    } catch (nextError) {
      setSubmitError(nextError.message || "评论发布失败。");
    } finally {
      setSubmitting("");
    }
  }

  async function submitReply(event) {
    event.preventDefault();
    const content = replyContent.trim();
    if (!content || !replyTarget || !onReply) return;
    setSubmitting(replyTarget);
    setSubmitError("");
    try {
      await onReply(content, replyTarget);
      setReplyTarget("");
      setReplyContent("");
    } catch (nextError) {
      setSubmitError(nextError.message || "回复发布失败。");
    } finally {
      setSubmitting("");
    }
  }

  if (collapsed) {
    return (
      <aside className="markdown-comments markdown-comments-collapsed" aria-label={`评论，共 ${totalCount} 条`}>
        <button
          type="button"
          className="markdown-comments-expand-button"
          aria-label="展开评论"
          title="展开评论"
          onClick={onToggleCollapsed}
        >
          <CommentPanelIcon collapsed />
          <span>{totalCount}</span>
        </button>
      </aside>
    );
  }

  return (
    <aside className="markdown-comments" aria-label={`评论，共 ${totalCount} 条`}>
      <header className="markdown-comments-header">
        <div className="markdown-comments-title">
          <h2>评论</h2>
          <span className="markdown-comments-count">{totalCount}</span>
        </div>
        <button
          type="button"
          className="markdown-comments-toggle"
          aria-label="收起评论"
          title="收起评论"
          onClick={onToggleCollapsed}
        >
          <CommentPanelIcon />
        </button>
      </header>
      <div className="markdown-comment-list">
        {loading ? <div className="markdown-comments-state">正在加载评论…</div> : null}
        {!loading && !comments.length ? <div className="markdown-comments-state">还没有评论</div> : null}
        {comments.map((comment, index) => (
          <div key={comment.commentId || `${comment.author}-${comment.createdAt}-${index}`} className="markdown-comment-thread">
            <CommentCard
              comment={comment}
              markdownComponents={markdownComponents}
              onReply={onReply ? (commentId) => {
                setReplyTarget(commentId);
                setReplyContent("");
                setSubmitError("");
              } : null}
            />
            {(comment.replies || []).map((reply, replyIndex) => (
              <CommentCard
                key={reply.commentId || `${reply.author}-${reply.createdAt}-${replyIndex}`}
                comment={reply}
                markdownComponents={markdownComponents}
                isReply
              />
            ))}
            {replyTarget === comment.commentId ? (
              <form className="markdown-comment-reply-form" onSubmit={submitReply}>
                <textarea
                  value={replyContent}
                  onChange={(event) => setReplyContent(event.target.value)}
                  placeholder="回复这条评论…"
                  rows={3}
                  autoFocus
                  disabled={submitting === replyTarget}
                />
                <div className="markdown-comment-form-actions">
                  <button type="button" onClick={() => setReplyTarget("")} disabled={submitting === replyTarget}>取消</button>
                  <button type="submit" disabled={!replyContent.trim() || submitting === replyTarget}>
                    {submitting === replyTarget ? "发送中…" : "回复"}
                  </button>
                </div>
              </form>
            ) : null}
          </div>
        ))}
      </div>
      {error || submitError ? <div className="markdown-comments-error">{submitError || error}</div> : null}
      {onCreate ? (
        <form className="markdown-comment-composer" onSubmit={submitComment}>
          <textarea
            value={newComment}
            onChange={(event) => setNewComment(event.target.value)}
            placeholder="添加评论…"
            rows={1}
            disabled={submitting === "new"}
          />
          <button type="submit" disabled={!newComment.trim() || submitting === "new"}>
            {submitting === "new" ? "发布中…" : "发布评论"}
          </button>
        </form>
      ) : null}
    </aside>
  );
}

function DiagramBlock({ language, source }) {
  if (language === "mermaid") {
    return <MermaidBlock source={source} />;
  }

  return <PlantUmlBlock source={source} />;
}

function useExpandedPreviewOverlay(expanded, anchorRef, setExpanded) {
  const [overlayBounds, setOverlayBounds] = useState(null);

  useEffect(() => {
    if (!expanded) {
      return undefined;
    }

    function handleKeyDown(event) {
      if (event.key === "Escape") {
        setExpanded(false);
      }
    }

    window.addEventListener("keydown", handleKeyDown);

    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [expanded, setExpanded]);

  useEffect(() => {
    if (!expanded) {
      setOverlayBounds(null);
      return undefined;
    }

    const assistantShell = anchorRef.current?.closest(".assistant-shell");
    const chatHeader = assistantShell?.querySelector(".chat-header");

    function updateOverlayBounds() {
      if (!assistantShell || !chatHeader) {
        setOverlayBounds({
          left: 0,
          top: 0,
          width: window.innerWidth,
          height: window.innerHeight
        });
        return;
      }

      const shellRect = assistantShell.getBoundingClientRect();
      const headerRect = chatHeader.getBoundingClientRect();
      const top = Math.max(shellRect.top, headerRect.bottom);
      const bottom = shellRect.bottom;

      setOverlayBounds({
        left: shellRect.left,
        top,
        width: shellRect.width,
        height: Math.max(320, bottom - top)
      });
    }

    updateOverlayBounds();
    window.addEventListener("resize", updateOverlayBounds);
    window.addEventListener("scroll", updateOverlayBounds, true);

    const resizeObserver =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(() => {
            updateOverlayBounds();
          });

    if (resizeObserver && assistantShell && chatHeader) {
      resizeObserver.observe(assistantShell);
      resizeObserver.observe(chatHeader);
    }

    return () => {
      window.removeEventListener("resize", updateOverlayBounds);
      window.removeEventListener("scroll", updateOverlayBounds, true);
      resizeObserver?.disconnect();
    };
  }, [anchorRef, expanded]);

  return overlayBounds;
}

function downloadPreviewAsset(source, filename, type) {
  const blob = new Blob([source], { type });
  const url = window.URL.createObjectURL(blob);
  const link = document.createElement("a");

  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => window.URL.revokeObjectURL(url), 0);
}

function MarkdownImage({ src, alt, ...props }) {
  const [expanded, setExpanded] = useState(false);
  const dialogTitleId = useId();
  const closeButtonRef = useRef(null);

  useEffect(() => {
    if (!expanded || typeof document === "undefined") {
      return undefined;
    }

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeButtonRef.current?.focus();

    function handleKeyDown(event) {
      if (event.key === "Escape") {
        setExpanded(false);
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [expanded]);

  const previewLabel = alt || "图片预览";
  const expandedOverlay =
    expanded && typeof document !== "undefined"
      ? createPortal(
          <div
            className="markdown-artifact-overlay markdown-image-lightbox"
            role="dialog"
            aria-modal="true"
            aria-labelledby={dialogTitleId}
          >
            <div className="markdown-image-lightbox-panel">
              <div className="markdown-artifact-toolbar">
                <span id={dialogTitleId} className="markdown-image-lightbox-title">
                  {previewLabel}
                </span>
                <div className="markdown-artifact-controls">
                  <a
                    className="markdown-artifact-icon-button"
                    href={src}
                    download
                    aria-label="下载图片"
                    title="下载图片"
                  >
                    <ActionIcon kind="download" />
                  </a>
                  <button
                    ref={closeButtonRef}
                    type="button"
                    className="markdown-artifact-icon-button"
                    aria-label="关闭图片预览"
                    title="关闭图片预览"
                    onClick={() => setExpanded(false)}
                  >
                    <ActionIcon kind="close" />
                  </button>
                </div>
              </div>
              <div
                className="markdown-image-lightbox-canvas"
                onMouseDown={(event) => {
                  if (event.target === event.currentTarget) {
                    setExpanded(false);
                  }
                }}
              >
                <img src={src} alt={alt || ""} />
              </div>
            </div>
          </div>,
          document.body
        )
      : null;

  return (
    <span className="markdown-image-shell">
      <button
        type="button"
        className="markdown-image-preview-button"
        aria-label={`放大查看：${previewLabel}`}
        title="点击放大"
        onClick={() => setExpanded(true)}
      >
        <img {...props} className="markdown-image" src={src} alt={alt || ""} loading="lazy" />
      </button>
      <span className="markdown-image-actions">
        <button
          type="button"
          className="markdown-artifact-icon-button"
          aria-label="放大图片"
          title="放大图片"
          onClick={() => setExpanded(true)}
        >
          <ActionIcon kind="maximize" />
        </button>
        <a
          className="markdown-artifact-icon-button"
          href={src}
          download
          aria-label="下载图片"
          title="下载图片"
        >
          <ActionIcon kind="download" />
        </a>
      </span>
      {expandedOverlay}
    </span>
  );
}

function MermaidBlock({ source }) {
  const elementId = useId().replace(/:/g, "-");
  const [state, setState] = useState({ status: "loading", svg: "", error: "", staleAssets: false });
  const [expanded, setExpanded] = useState(false);
  const diagramRef = useRef(null);
  const overlayBounds = useExpandedPreviewOverlay(expanded, diagramRef, setExpanded);
  const expandedLabel = expanded ? "收起预览" : "扩大展示";

  useEffect(() => {
    let cancelled = false;

    setExpanded(false);
    setState({ status: "loading", svg: "", error: "", staleAssets: false });

    loadMermaid()
      .then(async (mermaid) => {
        const renderId = `mermaid-${elementId}-${Date.now()}`;
        const { svg } = await mermaid.render(renderId, source);
        if (!cancelled) {
          setState({ status: "ready", svg, error: "", staleAssets: false });
        }
      })
      .catch((error) => {
        if (!cancelled) {
          const errorMessage = error instanceof Error ? error.message : "Mermaid 渲染失败";
          setState({
            status: "error",
            svg: "",
            error: errorMessage,
            staleAssets: /Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed/i.test(
              errorMessage
            )
          });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [elementId, source]);

  function handleDownload() {
    if (!state.svg) {
      return;
    }

    downloadPreviewAsset(state.svg, "mermaid-diagram.svg", "image/svg+xml;charset=utf-8");
  }

  function renderToolbar() {
    return (
      <div className="markdown-artifact-toolbar">
        <div className="markdown-artifact-title">
          <span className="markdown-artifact-label">mermaid</span>
          <span className="markdown-artifact-caption">Mermaid 预览</span>
        </div>
        <div className="markdown-artifact-controls">
          <button
            type="button"
            className={`markdown-artifact-icon-button ${expanded ? "markdown-artifact-icon-button-active" : ""}`.trim()}
            aria-label={expandedLabel}
            title={expandedLabel}
            aria-pressed={expanded}
            onClick={() => setExpanded((current) => !current)}
          >
            <ActionIcon kind={expanded ? "minimize" : "maximize"} />
          </button>
          <CopyIconButton
            value={source}
            idleLabel="复制 Mermaid"
            successLabel="Mermaid 已复制"
            className="markdown-artifact-copy"
            buttonClassName="markdown-artifact-icon-button"
            successClassName="markdown-artifact-icon-button-success"
            errorClassName="markdown-artifact-icon-button-error"
          />
          <button
            type="button"
            className="markdown-artifact-icon-button"
            aria-label="下载 Mermaid SVG"
            title="下载 Mermaid SVG"
            onClick={handleDownload}
          >
            <ActionIcon kind="download" />
          </button>
        </div>
      </div>
    );
  }

  function renderDiagramContent() {
    return <div className="markdown-diagram-canvas" dangerouslySetInnerHTML={{ __html: state.svg }} />;
  }

  if (state.status === "ready") {
    const expandedOverlay =
      expanded && overlayBounds && typeof document !== "undefined"
        ? createPortal(
            <div
              className="markdown-artifact-overlay"
              role="dialog"
              aria-label="Mermaid 扩大预览"
              style={{
                left: `${overlayBounds.left}px`,
                top: `${overlayBounds.top}px`,
                width: `${overlayBounds.width}px`,
                height: `${overlayBounds.height}px`
              }}
            >
              <div className="markdown-diagram-block markdown-diagram-block-expanded markdown-diagram-overlay-panel">
                {renderToolbar()}
                {renderDiagramContent()}
              </div>
            </div>,
            document.body
          )
        : null;

    return (
      <div ref={diagramRef} className="markdown-diagram-block markdown-diagram-mermaid">
        {renderToolbar()}
        {renderDiagramContent()}
        {expandedOverlay}
      </div>
    );
  }

  return (
    <div className="markdown-diagram-block markdown-diagram-mermaid">
      <div className="markdown-diagram-label">mermaid</div>
      <div className="markdown-diagram-placeholder">
        {state.status === "loading" ? "正在渲染 Mermaid 图表..." : `Mermaid 渲染失败：${state.error}`}
        {state.staleAssets ? (
          <button type="button" className="markdown-diagram-reload" onClick={() => window.location.reload()}>
            刷新页面后重试
          </button>
        ) : null}
      </div>
    </div>
  );
}

function PlantUmlBlock({ source }) {
  const encodedSource = encode(source);
  const svgUrl = `https://www.plantuml.com/plantuml/svg/${encodedSource}`;

  return (
    <div className="markdown-diagram-block markdown-diagram-plantuml">
      <div className="markdown-diagram-label">plantuml</div>
      <div className="markdown-diagram-canvas">
        <img className="markdown-diagram-image" src={svgUrl} alt="PlantUML 图表" loading="lazy" />
      </div>
    </div>
  );
}

const ArtifactPreviewFrame = memo(function ArtifactPreviewFrame({ previewSource }) {
  return (
    <iframe
      className="markdown-artifact-frame"
      title="HTML 预览"
      sandbox="allow-scripts allow-forms allow-modals allow-popups"
      referrerPolicy="no-referrer"
      srcDoc={previewSource}
    />
  );
});

function HtmlArtifactBlock({ language, source }) {
  const [mode, setMode] = useState("preview");
  const [expanded, setExpanded] = useState(false);
  const artifactRef = useRef(null);
  const label = language || "html";
  const normalizedLanguage = label.trim().toLowerCase();
  const artifactKindLabel = normalizedLanguage === "svg" ? "SVG" : "HTML";
  const expandedLabel = expanded ? "收起预览" : "扩大展示";
  const overlayBounds = useExpandedPreviewOverlay(expanded, artifactRef, setExpanded);
  const displaySource = useMemo(
    () => (normalizedLanguage === "svg" ? normalizeSvgArtifactSource(source) : source),
    [normalizedLanguage, source]
  );
  const previewSource = useMemo(() => buildHtmlPreviewDocument(displaySource), [displaySource]);

  function handleDownload() {
    const isSvgArtifact = normalizedLanguage === "svg";
    downloadPreviewAsset(
      isSvgArtifact ? displaySource : previewSource,
      isSvgArtifact ? "artifact.svg" : "artifact.html",
      isSvgArtifact ? "image/svg+xml;charset=utf-8" : "text/html;charset=utf-8"
    );
  }

  function renderToolbar() {
    return (
      <div className="markdown-artifact-toolbar">
        <div className="markdown-artifact-title">
          <span className="markdown-artifact-label">{label}</span>
          <span className="markdown-artifact-caption">{artifactKindLabel} 预览</span>
        </div>
        <div className="markdown-artifact-controls">
          <div className="markdown-artifact-tabs" role="tablist" aria-label={`${artifactKindLabel} 显示方式`}>
            <button
              type="button"
              role="tab"
              className={`markdown-artifact-tab ${mode === "preview" ? "markdown-artifact-tab-active" : ""}`.trim()}
              aria-selected={mode === "preview"}
              onClick={() => setMode("preview")}
            >
              预览
            </button>
            <button
              type="button"
              role="tab"
              className={`markdown-artifact-tab ${mode === "source" ? "markdown-artifact-tab-active" : ""}`.trim()}
              aria-selected={mode === "source"}
              onClick={() => setMode("source")}
            >
              源码
            </button>
          </div>
          <button
            type="button"
            className={`markdown-artifact-icon-button ${expanded ? "markdown-artifact-icon-button-active" : ""}`.trim()}
            aria-label={expandedLabel}
            title={expandedLabel}
            aria-pressed={expanded}
            onClick={() => setExpanded((current) => !current)}
          >
            <ActionIcon kind={expanded ? "minimize" : "maximize"} />
          </button>
          <CopyIconButton
            value={displaySource}
            idleLabel={`复制 ${artifactKindLabel}`}
            successLabel={`${artifactKindLabel} 已复制`}
            className="markdown-artifact-copy"
            buttonClassName="markdown-artifact-icon-button"
            successClassName="markdown-artifact-icon-button-success"
            errorClassName="markdown-artifact-icon-button-error"
          />
          <button
            type="button"
            className="markdown-artifact-icon-button"
            aria-label={`下载 ${artifactKindLabel}`}
            title={`下载 ${artifactKindLabel}`}
            onClick={handleDownload}
          >
            <ActionIcon kind="download" />
          </button>
        </div>
      </div>
    );
  }

  function renderArtifactContent() {
    return mode === "preview" ? (
      <div className="markdown-artifact-preview">
        <ArtifactPreviewFrame previewSource={previewSource} />
      </div>
    ) : (
      <pre className="markdown-pre markdown-artifact-source">
        <code className={language ? `language-${language}` : undefined}>{displaySource}</code>
      </pre>
    );
  }

  const expandedOverlay =
    expanded && overlayBounds && typeof document !== "undefined"
      ? createPortal(
          <div
            className="markdown-artifact-overlay"
            role="dialog"
            aria-label="Artifact 扩大预览"
            style={{
              left: `${overlayBounds.left}px`,
              top: `${overlayBounds.top}px`,
              width: `${overlayBounds.width}px`,
              height: `${overlayBounds.height}px`
            }}
          >
            <div className="markdown-artifact-block markdown-artifact-block-expanded markdown-artifact-overlay-panel">
              {renderToolbar()}
              {renderArtifactContent()}
            </div>
          </div>,
          document.body
        )
      : null;

  return (
    <div ref={artifactRef} className="markdown-artifact-block">
      {renderToolbar()}

      {renderArtifactContent()}
      {expandedOverlay}
    </div>
  );
}

function MarkdownRenderer({
  markdown = "",
  className = "",
  streaming = false,
  variant = "chat",
  resolveImageSrc = null,
  resolveLinkHref = null,
  requirementReferences = null,
  documentHeader = null,
  documentComments = null,
  commentsLoading = false,
  commentsError = "",
  onCreateComment = null,
  onReplyComment = null
}) {
  const [commentsCollapsed, setCommentsCollapsed] = useState(false);
  const normalizedSource = markdown.replace(/\r\n/g, "\n").trim();
  const documentParts =
    variant === "document"
      ? parseDocumentMetadataSection(normalizedSource)
      : { content: normalizedSource, metadataItems: [], metadataLabel: "" };
  const commentParts =
    variant === "document"
      ? splitDocumentComments(documentParts.content)
      : { content: documentParts.content, comments: [] };
  const normalized = commentParts.content;
  const displayedComments = Array.isArray(documentComments) ? documentComments : commentParts.comments;
  const commentsEnabled = variant === "document" && (Array.isArray(documentComments) || displayedComments.length > 0);
  const remarkPlugins = useMemo(
    () =>
      requirementReferences?.size
        ? [...baseRemarkPlugins, [remarkRequirementReferences, { references: requirementReferences }]]
        : baseRemarkPlugins,
    [requirementReferences]
  );

  const markdownComponents = useMemo(
    () => ({
      a({ href, children, ...props }) {
        const resolvedHref = typeof resolveLinkHref === "function" ? resolveLinkHref(href || "") : href;
        const isExternalLink = isExternalHref(resolvedHref);
        return (
          <a
            {...props}
            className="markdown-link"
            href={resolvedHref}
            target={isExternalLink ? "_blank" : undefined}
            rel={isExternalLink ? "noreferrer" : undefined}
          >
            {children}
          </a>
        );
      },
      img({ src, alt, ...props }) {
        const resolvedSrc = typeof resolveImageSrc === "function" ? resolveImageSrc(src || "") : src;
        return <MarkdownImage {...props} src={resolvedSrc} alt={alt || ""} />;
      },
      code({ node, className: codeClassName, children, ...props }) {
        const language = codeClassName?.replace(/^language-/, "") || "";
        const rawText = String(children).replace(/\n$/, "");
        const isBlock = isBlockCodeNode(node, codeClassName, rawText);

        if (!isBlock) {
          const resolvedCodeHref = typeof resolveLinkHref === "function" ? resolveLinkHref(rawText) : rawText;
          if (resolvedCodeHref && resolvedCodeHref !== rawText) {
            return (
              <a
                {...props}
                className="markdown-inline-code markdown-inline-download-link"
                href={resolvedCodeHref}
                title="下载文件"
              >
                {children}
              </a>
            );
          }
          return (
            <code {...props} className="markdown-inline-code">
              {children}
            </code>
          );
        }

        if (variant === "chat" && !language && looksLikePathSnippet(rawText)) {
          return (
            <div className="markdown-path-chip">
              <code {...props} className="markdown-path-chip-text">
                {rawText}
              </code>
            </div>
          );
        }

        if (DIAGRAM_LANGUAGES.has(language)) {
          return <DiagramBlock language={language} source={rawText} />;
        }

        if (variant === "chat" && looksLikeHtmlArtifact(rawText, language)) {
          return <HtmlArtifactBlock language={language || "html"} source={rawText} />;
        }

        return (
          <div className="markdown-code-block">
            {language ? <div className="markdown-code-label">{language}</div> : null}
            <pre className="markdown-pre">
              <code {...props} className={codeClassName}>
                {children}
              </code>
            </pre>
          </div>
        );
      },
      table({ children, ...props }) {
        return (
          <div className="markdown-table-wrap">
            <table {...props} className="markdown-table">
              {children}
            </table>
          </div>
        );
      },
      li({ node: _node, children, className: liClassName, ...props }) {
        const classes = [];
        if (liClassName) {
          classes.push(liClassName);
        }
        if (variant === "document" && looksLikeReviewFindingTitle(children)) {
          classes.push("markdown-review-finding-title");
        }
        return (
          <li {...props} className={classes.length ? classes.join(" ") : undefined}>
            {children}
          </li>
        );
      }
    }),
    [resolveImageSrc, resolveLinkHref, variant]
  );

  if (!normalized && !commentsEnabled && !documentHeader) {
    return null;
  }

  const classes = ["markdown-body", `markdown-body-${variant}`];

  if (className) {
    classes.push(className);
  }

  if (streaming) {
    classes.push("markdown-body-streaming");
  }

  if (commentsEnabled) {
    classes.push("markdown-body-has-comments");
    if (commentsCollapsed) {
      classes.push("markdown-body-comments-collapsed");
    }
  }

  const markdownSections = normalized && variant === "document"
      ? parseCustomFieldsSection(normalized)
      : normalized
        ? [{ type: "markdown", content: normalized }]
        : [];

  const documentContent = (
    <>
      {documentHeader}
      {variant === "document" && documentParts.metadataItems.length ? (
        <MetadataSection
          label={documentParts.metadataLabel}
          items={documentParts.metadataItems}
          resolveLinkHref={resolveLinkHref}
        />
      ) : null}
      {markdownSections.map((section, index) =>
        section.type === "metadata" ? (
          <Fragment key={`${section.label}-${index}`}>
            {section.label ? <h2>{section.label}</h2> : null}
            <MetadataSection
              label={section.label}
              items={section.items}
              resolveLinkHref={resolveLinkHref}
            />
          </Fragment>
        ) : (
          <ReactMarkdown
            key={`markdown-${index}`}
            rehypePlugins={rehypePlugins}
            remarkPlugins={remarkPlugins}
            components={markdownComponents}
          >
            {section.content}
          </ReactMarkdown>
        )
      )}
    </>
  );

  return (
    <div className={classes.join(" ")}>
      {commentsEnabled ? (
        <>
          <div className="markdown-document-content">{documentContent}</div>
          <CommentSection
            comments={displayedComments}
            markdownComponents={markdownComponents}
            collapsed={commentsCollapsed}
            onToggleCollapsed={() => setCommentsCollapsed((current) => !current)}
            loading={commentsLoading}
            error={commentsError}
            onCreate={onCreateComment}
            onReply={onReplyComment}
          />
        </>
      ) : documentContent}
    </div>
  );
}

export default memo(MarkdownRenderer);
