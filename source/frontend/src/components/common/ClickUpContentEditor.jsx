import { useEffect, useId, useRef, useState } from "react";
import { DocoTextEditor } from "doco-text-editor";
import "doco-text-editor/style.css";
import {
  getRequirementClickUpContent,
  listRequirementClickUpContentHistory,
  restoreRequirementClickUpContentHistory,
  updateRequirementClickUpContent
} from "../../services/workspaceApi";
import {
  clickUpMarkdownToEditorMarkdown,
  editorMarkdownToClickUpMarkdown
} from "../../utils/clickupContentAdapter";

export default function ClickUpContentEditor({
  path,
  onCancel,
  onSaved,
  resolveImageSrc = null
}) {
  const editorRef = useRef(null);
  const confirmationTitleId = useId();
  const confirmationDescriptionId = useId();
  const [remoteContent, setRemoteContent] = useState(null);
  const [contentChanged, setContentChanged] = useState(false);
  const [sourceType, setSourceType] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyVersions, setHistoryVersions] = useState([]);
  const [restoringVersionId, setRestoringVersionId] = useState("");
  const [saveConfirmationOpen, setSaveConfirmationOpen] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let disposed = false;
    setRemoteContent(null);
    setContentChanged(false);
    setSourceType("");
    setLoading(true);
    setHistoryOpen(false);
    setHistoryVersions([]);
    setRestoringVersionId("");
    setSaveConfirmationOpen(false);
    setError("");

    getRequirementClickUpContent(path)
      .then((payload) => {
        if (disposed) return;
        setRemoteContent(payload.content || "");
        setSourceType(payload.source_type || "");
      })
      .catch((nextError) => {
        if (!disposed) setError(nextError.message || "ClickUp 正文加载失败。");
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });

    return () => {
      disposed = true;
    };
  }, [path]);

  useEffect(() => {
    if (!saveConfirmationOpen) return undefined;

    function handleKeyDown(event) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      setSaveConfirmationOpen(false);
    }

    document.addEventListener("keydown", handleKeyDown, true);
    return () => document.removeEventListener("keydown", handleKeyDown, true);
  }, [saveConfirmationOpen]);

  async function handleSave() {
    setSaveConfirmationOpen(false);
    const content = getCurrentClickUpContent();
    if (content === null) {
      setError("编辑器尚未准备完成，请稍后再试。");
      return;
    }
    if (content === remoteContent) {
      setContentChanged(false);
      setError("正文未修改，无需保存。");
      return;
    }

    setSaving(true);
    setError("");
    try {
      const payload = await updateRequirementClickUpContent(path, content, remoteContent);
      onSaved?.(payload);
    } catch (nextError) {
      setError(nextError.message || "保存到 ClickUp 失败。");
    } finally {
      setSaving(false);
    }
  }

  function getCurrentClickUpContent() {
    const editorContent = editorRef.current?.getContent("markdown");
    return typeof editorContent === "string" ? editorMarkdownToClickUpMarkdown(editorContent) : null;
  }

  function handleEditorChange() {
    const content = getCurrentClickUpContent();
    if (content !== null) {
      setContentChanged(content !== remoteContent);
    }
  }

  function handleOpenSaveConfirmation() {
    const content = getCurrentClickUpContent();
    if (content === null) {
      setError("编辑器尚未准备完成，请稍后再试。");
      return;
    }
    if (content === remoteContent) {
      setContentChanged(false);
      setError("正文未修改，无需保存。");
      return;
    }
    setError("");
    setSaveConfirmationOpen(true);
  }

  async function handleHistoryToggle() {
    if (historyOpen) {
      setHistoryOpen(false);
      return;
    }
    setHistoryLoading(true);
    setError("");
    try {
      const payload = await listRequirementClickUpContentHistory(path);
      setHistoryVersions(payload.versions || []);
      setHistoryOpen(true);
    } catch (nextError) {
      setError(nextError.message || "历史版本加载失败。");
    } finally {
      setHistoryLoading(false);
    }
  }

  async function handleRestore(version) {
    if (restoringVersionId || saving) return;
    if (!window.confirm("恢复后会覆盖 ClickUp 当前正文，并自动保留当前版本。确定恢复吗？")) return;

    setRestoringVersionId(version.version_id);
    setError("");
    try {
      const payload = await restoreRequirementClickUpContentHistory(path, version.version_id);
      onSaved?.(payload);
    } catch (nextError) {
      setError(nextError.message || "恢复历史版本失败。");
    } finally {
      setRestoringVersionId("");
    }
  }

  return (
    <section className="clickup-content-editor" aria-label="编辑 ClickUp 正文">
      <header className="clickup-content-editor-head">
        <div>
          <strong>{sourceType === "task" ? "编辑 Task 描述" : "编辑需求正文"}</strong>
          <span>保存会以 Markdown 替换 ClickUp 当前正文；流程图/UML 保存为图片并保留源码，内嵌表格保存为普通表格并保留标记</span>
        </div>
        <div className="clickup-content-editor-actions">
          <button type="button" className="button button-secondary" disabled={saving} onClick={onCancel}>
            取消
          </button>
          <button
            type="button"
            className="button button-secondary"
            disabled={loading || saving || historyLoading || remoteContent === null}
            onClick={() => void handleHistoryToggle()}
          >
            {historyLoading ? "正在加载…" : historyOpen ? "收起历史" : "历史版本"}
          </button>
          <button
            type="button"
            className="button button-primary"
            disabled={loading || saving || remoteContent === null || !contentChanged}
            aria-busy={saving ? "true" : undefined}
            title={contentChanged ? "保存到 ClickUp 并同步本地 Markdown" : "请先修改正文"}
            onClick={handleOpenSaveConfirmation}
          >
            {saving ? "正在保存…" : "保存并同步"}
          </button>
        </div>
      </header>

      {loading ? <div className="clickup-content-editor-state">正在读取 ClickUp 最新正文…</div> : null}
      {error ? <div className="clickup-content-editor-error" role="alert">{error}</div> : null}
      {historyOpen ? (
        <section className="clickup-content-history" aria-label="历史版本">
          <header>
            <strong>历史版本</strong>
            <span>恢复时会先自动保留当前 ClickUp 正文。</span>
          </header>
          {historyVersions.length ? (
            <ul>
              {historyVersions.map((version) => (
                <li key={version.version_id}>
                  <div>
                    <strong>{new Date(version.created_at).toLocaleString()}</strong>
                    <span>{version.title || "未命名文档"} · {version.content_length} 字符</span>
                  </div>
                  <button
                    type="button"
                    className="button button-secondary"
                    disabled={Boolean(restoringVersionId) || saving}
                    onClick={() => void handleRestore(version)}
                  >
                    {restoringVersionId === version.version_id ? "正在恢复…" : "恢复"}
                  </button>
                </li>
              ))}
            </ul>
          ) : <p>暂无历史版本。首次覆盖保存后会在这里保留当前版本。</p>}
        </section>
      ) : null}
      {!loading && remoteContent !== null ? (
        <DocoTextEditor
          ref={editorRef}
          defaultValue={clickUpMarkdownToEditorMarkdown(remoteContent)}
          format="markdown"
          placeholder="输入需求正文…"
          uploadImage={null}
          resolveImageSrc={resolveImageSrc || undefined}
          showStatusBar
          onChange={handleEditorChange}
          onError={(nextError) => setError(nextError.message || "编辑器发生错误。")}
        />
      ) : null}

      {saveConfirmationOpen ? (
        <div
          className="clickup-save-confirm-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            event.stopPropagation();
            setSaveConfirmationOpen(false);
          }}
        >
          <section
            className="clickup-save-confirm-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby={confirmationTitleId}
            aria-describedby={confirmationDescriptionId}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <span className="clickup-save-confirm-eyebrow">覆盖提醒</span>
            <h3 id={confirmationTitleId}>
              确认覆盖 ClickUp {sourceType === "task" ? "Task 描述" : "文档正文"}？
            </h3>
            <p id={confirmationDescriptionId}>
              保存后，当前编辑内容将完整替换 ClickUp 原有正文。此操作无法自动撤销，请确认需要保留的内容已包含在当前编辑版本中。
            </p>
            <div className="clickup-save-confirm-actions">
              <button
                type="button"
                className="button button-secondary"
                onClick={() => setSaveConfirmationOpen(false)}
                autoFocus
              >
                返回编辑
              </button>
              <button type="button" className="button button-primary" onClick={() => void handleSave()}>
                确认覆盖并保存
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </section>
  );
}
