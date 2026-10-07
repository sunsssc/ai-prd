import { useEffect, useState, startTransition } from "react";
import { PrismLight as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import jsx from "react-syntax-highlighter/dist/esm/languages/prism/jsx";
import typescript from "react-syntax-highlighter/dist/esm/languages/prism/typescript";
import tsx from "react-syntax-highlighter/dist/esm/languages/prism/tsx";
import css from "react-syntax-highlighter/dist/esm/languages/prism/css";
import markup from "react-syntax-highlighter/dist/esm/languages/prism/markup";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import yaml from "react-syntax-highlighter/dist/esm/languages/prism/yaml";
import toml from "react-syntax-highlighter/dist/esm/languages/prism/toml";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import go from "react-syntax-highlighter/dist/esm/languages/prism/go";
import rust from "react-syntax-highlighter/dist/esm/languages/prism/rust";
import java from "react-syntax-highlighter/dist/esm/languages/prism/java";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import MarkdownRenderer from "./MarkdownRenderer";
import PreviewToolbar from "./PreviewToolbar";
import { createRequirementComment, listRequirementComments } from "../../services/workspaceApi";

SyntaxHighlighter.registerLanguage("python", python);
SyntaxHighlighter.registerLanguage("javascript", javascript);
SyntaxHighlighter.registerLanguage("jsx", jsx);
SyntaxHighlighter.registerLanguage("typescript", typescript);
SyntaxHighlighter.registerLanguage("tsx", tsx);
SyntaxHighlighter.registerLanguage("css", css);
SyntaxHighlighter.registerLanguage("html", markup);
SyntaxHighlighter.registerLanguage("xml", markup);
SyntaxHighlighter.registerLanguage("json", json);
SyntaxHighlighter.registerLanguage("yaml", yaml);
SyntaxHighlighter.registerLanguage("toml", toml);
SyntaxHighlighter.registerLanguage("bash", bash);
SyntaxHighlighter.registerLanguage("go", go);
SyntaxHighlighter.registerLanguage("rust", rust);
SyntaxHighlighter.registerLanguage("java", java);
SyntaxHighlighter.registerLanguage("sql", sql);

const EXT_TO_LANGUAGE = {
  py: "python",
  js: "javascript",
  jsx: "jsx",
  ts: "typescript",
  tsx: "tsx",
  css: "css",
  html: "html",
  json: "json",
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
  sh: "bash",
  go: "go",
  rs: "rust",
  java: "java",
  sql: "sql",
  xml: "xml",
};

function detectLanguage(filename = "") {
  const ext = filename.split(".").pop().toLowerCase();
  return EXT_TO_LANGUAGE[ext] || "text";
}

const plainCodeStyle = {
  fontFamily: "var(--font-mono)",
  fontSize: "0.92rem",
  lineHeight: 1.75,
  padding: "20px",
  margin: 0,
  whiteSpace: "pre",
  overflowX: "auto",
  color: "#4f4841",
};

function CodeHighlighter({ language, content }) {
  const [highlighted, setHighlighted] = useState(false);

  useEffect(() => {
    setHighlighted(false);
    const id = setTimeout(() => {
      startTransition(() => setHighlighted(true));
    }, 200);
    return () => clearTimeout(id);
  }, [content]);

  if (!highlighted) {
    return <pre style={plainCodeStyle}>{content}</pre>;
  }

  return (
    <SyntaxHighlighter
      language={language}
      style={oneLight}
      customStyle={{ margin: 0, padding: "20px", background: "transparent", fontSize: "0.92rem", lineHeight: 1.75 }}
      codeTagProps={{ style: { fontFamily: "var(--font-mono)" } }}
      wrapLongLines={false}
    >
      {content}
    </SyntaxHighlighter>
  );
}

function ToolbarActionIcon({ kind }) {
  const commonProps = {
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: "1.9",
    strokeLinecap: "round",
    strokeLinejoin: "round",
    className: "toolbar-icon",
    "aria-hidden": "true"
  };

  if (kind === "save") {
    return (
      <svg {...commonProps}>
        <path d="m5.5 12.5 4.2 4.2L18.5 8" />
      </svg>
    );
  }

  return (
    <svg {...commonProps}>
      <path d="M6.5 6.5 17.5 17.5" />
      <path d="M17.5 6.5 6.5 17.5" />
    </svg>
  );
}

function extractLeadingMarkdownTitle(markdown = "") {
  const normalized = markdown.replace(/\r\n/g, "\n");
  const match = normalized.match(/^\s*#\s+(.+?)\s*(?:\n+|$)/);

  if (!match) {
    return {
      title: "",
      content: markdown
    };
  }

  return {
    title: match[1].trim(),
    content: normalized.slice(match[0].length).replace(/^\n+/, "")
  };
}

function extractSkillMarkdownHeader(markdown = "") {
  const normalized = markdown.replace(/\r\n/g, "\n");
  const match = normalized.match(/^---\n([\s\S]*?)\n---(?:\n+|$)/);

  if (!match) {
    return {
      title: "",
      description: "",
      content: markdown
    };
  }

  const metadata = {};

  for (const line of match[1].split("\n")) {
    const separatorIndex = line.indexOf(":");
    if (separatorIndex === -1) {
      continue;
    }

    const key = line.slice(0, separatorIndex).trim().toLowerCase();
    const value = line.slice(separatorIndex + 1).trim();
    if (key) {
      metadata[key] = value;
    }
  }

  if (!metadata.name && !metadata.description) {
    return {
      title: "",
      description: "",
      content: markdown
    };
  }

  return {
    title: metadata.name || "",
    description: metadata.description || "",
    content: normalized.slice(match[0].length).replace(/^\n+/, "")
  };
}

function parseMarkdownDocument(markdown = "", headerMode = "default") {
  if (headerMode === "skill") {
    return extractSkillMarkdownHeader(markdown);
  }

  const document = extractLeadingMarkdownTitle(markdown);
  return {
    ...document,
    description: ""
  };
}

function normalizeClickUpComments(comments = []) {
  return comments.map((comment) => ({
    commentId: comment.comment_id,
    author: comment.author,
    createdAt: comment.created_at,
    content: comment.content,
    replies: normalizeClickUpComments(comment.replies || [])
  }));
}

export default function DocumentDetailPanel({
  node,
  fileUpdatedAt,
  fileContent,
  fileContentFormat = "plain",
  fileContentClassName = "",
  fileLanguage = "",
  isEditing = false,
  editable = false,
  onSave,
  onCancel,
  onContentChange,
  fileHeaderActions = null,
  inlineFileHeader = false,
  markdownHeaderMode = "default",
  folderSectionTitle = "当前目录内容",
  renderFolderChild,
  resolveImageSrc = null,
  resolveLinkHref = null,
  contentHeader = null,
  contentOverlay = null,
  clickUpCommentsPath = ""
}) {
  const [clickUpComments, setClickUpComments] = useState([]);
  const [commentsLoading, setCommentsLoading] = useState(false);
  const [commentsError, setCommentsError] = useState("");

  useEffect(() => {
    if (!clickUpCommentsPath) {
      setClickUpComments([]);
      setCommentsLoading(false);
      setCommentsError("");
      return undefined;
    }

    let disposed = false;
    setClickUpComments([]);
    setCommentsLoading(true);
    setCommentsError("");
    listRequirementComments(clickUpCommentsPath)
      .then((payload) => {
        if (!disposed) setClickUpComments(normalizeClickUpComments(payload.comments));
      })
      .catch((error) => {
        if (!disposed) setCommentsError(error.message || "评论加载失败。");
      })
      .finally(() => {
        if (!disposed) setCommentsLoading(false);
      });

    return () => {
      disposed = true;
    };
  }, [clickUpCommentsPath]);

  async function handleCreateComment(content, parentCommentId = null) {
    const payload = await createRequirementComment(clickUpCommentsPath, content, parentCommentId);
    setClickUpComments(normalizeClickUpComments(payload.comments));
    setCommentsError("");
  }

  if (node.kind === "folder") {
    const folderMarkdown = fileContent && fileContentFormat === "markdown" ? parseMarkdownDocument(fileContent, inlineFileHeader ? markdownHeaderMode : "default") : null;
    const folderTitle = folderMarkdown?.title || node.title || node.name;
    const folderDescription = folderMarkdown?.description || "";
    const folderRenderedMarkdown = folderMarkdown ? (inlineFileHeader ? folderMarkdown.content : fileContent) : null;

    return (
      <>
        <div className="detail-card detail-head">
          <div>
            <h2 className="detail-title">{folderTitle}</h2>
            {folderDescription ? <p className="document-inline-description">{folderDescription}</p> : null}
            {node.subtitle ? <div className="content-subline">{node.subtitle}</div> : node.path ? <div className="content-subline">{node.path}</div> : null}
          </div>
          {fileHeaderActions ? <div className="detail-head-actions">{fileHeaderActions}</div> : null}
        </div>

        {folderRenderedMarkdown ? (
          <div className="content-shell">
            {contentHeader}
            <div className={`content-view content-view-markdown ${fileContentClassName}`.trim()}>
              <MarkdownRenderer
                key={node.path}
                markdown={folderRenderedMarkdown}
                variant="document"
                resolveImageSrc={resolveImageSrc}
                resolveLinkHref={resolveLinkHref}
              />
            </div>
            {contentOverlay}
          </div>
        ) : fileContent ? (
          <div className="content-shell">
            {contentHeader}
            <pre className="content-view">{fileContent}</pre>
            {contentOverlay}
          </div>
        ) : null}

        {(node.children || []).length > 0 ? (
          <article className="detail-card">
            <h3 className="folder-section-title">{folderSectionTitle}</h3>
            <div className="child-grid" style={{ marginTop: 14 }}>
              {(node.children || []).map((child) => renderFolderChild(child))}
            </div>
          </article>
        ) : null}
      </>
    );
  }

  const shouldInlineFileHeader = inlineFileHeader && !isEditing;
  const markdownDocument = fileContentFormat === "markdown" ? parseMarkdownDocument(fileContent, markdownHeaderMode) : null;
  const inlineHeaderTitle = markdownDocument?.title || node.title || node.name;
  const inlineHeaderDescription = markdownDocument?.description || "";
  const renderedMarkdown = shouldInlineFileHeader && markdownDocument ? markdownDocument.content : fileContent;
  const codeLanguage = fileLanguage || detectLanguage(node.name);
  const editingHeaderActions = editable && isEditing ? (
    <>
      <button
        type="button"
        className="toolbar-icon-button document-edit-cancel-button"
        aria-label="取消编辑"
        title="取消编辑"
        onClick={onCancel}
      >
        <ToolbarActionIcon kind="cancel" />
      </button>
      <button
        type="button"
        className="toolbar-icon-button document-edit-save-button"
        aria-label="保存编辑"
        title="保存编辑"
        onClick={onSave}
      >
        <ToolbarActionIcon kind="save" />
      </button>
    </>
  ) : null;
  const headerActions = editingHeaderActions || fileHeaderActions;
  const hasFileHeader = Boolean(headerActions || fileUpdatedAt);

  return (
    <>
      {hasFileHeader ? <PreviewToolbar timestamp={fileUpdatedAt} actions={headerActions} /> : null}

      <div className={`content-shell ${editable && isEditing ? "content-shell-editing" : ""}`.trim()}>
        {contentHeader}
        {editable && isEditing ? (
          <textarea className="editor-area" value={fileContent} onChange={(event) => onContentChange?.(event.target.value)} />
        ) : fileContentFormat === "markdown" ? (
          <div className={`content-view content-view-markdown ${fileContentClassName}`.trim()}>
            <MarkdownRenderer
              key={node.path}
              markdown={renderedMarkdown}
              variant="document"
              resolveImageSrc={resolveImageSrc}
              resolveLinkHref={resolveLinkHref}
              documentHeader={shouldInlineFileHeader ? (
                <div className="document-inline-header">
                  <div className="document-inline-header-main">
                    <h1 className="detail-title document-inline-title">{inlineHeaderTitle}</h1>
                    {inlineHeaderDescription ? (
                      <p className="document-inline-description">{inlineHeaderDescription}</p>
                    ) : null}
                  </div>
                </div>
              ) : null}
              documentComments={clickUpCommentsPath ? clickUpComments : null}
              commentsLoading={commentsLoading}
              commentsError={commentsError}
              onCreateComment={clickUpCommentsPath ? handleCreateComment : null}
              onReplyComment={clickUpCommentsPath ? handleCreateComment : null}
            />
          </div>
        ) : fileContentFormat === "code" ? (
          <div className={`content-view content-view-code ${fileContentClassName}`.trim()}>
            <CodeHighlighter key={node.path} language={codeLanguage} content={fileContent} />
          </div>
        ) : (
          <pre className={`content-view ${fileContentClassName}`.trim()}>{fileContent}</pre>
        )}
        {contentOverlay}
      </div>
    </>
  );
}
