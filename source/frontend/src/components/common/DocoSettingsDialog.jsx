import { useEffect, useState } from "react";
import { deleteDocoSettings, getDocoSettings, updateDocoSettings } from "../../services/docoApi";

export default function DocoSettingsDialog({ open, onClose }) {
  const [settings, setSettings] = useState(null);
  const [apiToken, setApiToken] = useState("");
  const [defaultKnowledgeBaseId, setDefaultKnowledgeBaseId] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) {
      return undefined;
    }

    let cancelled = false;
    setLoading(true);
    setError("");
    getDocoSettings()
      .then((payload) => {
        if (cancelled) {
          return;
        }
        setSettings(payload);
        setApiToken("");
        setDefaultKnowledgeBaseId(payload?.default_knowledge_base_id ? String(payload.default_knowledge_base_id) : "");
      })
      .catch((requestError) => {
        if (!cancelled) {
          setError(requestError.message || "加载 Doco 设置失败。");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [open]);

  if (!open) {
    return null;
  }

  async function handleSave(event) {
    event.preventDefault();
    const normalizedToken = apiToken.trim();
    if (!normalizedToken && !settings?.configured) {
      setError("请输入你的 Doco Token。");
      return;
    }

    setSaving(true);
    setError("");
    try {
      const payload = await updateDocoSettings({
        apiToken: normalizedToken,
        defaultKnowledgeBaseId: defaultKnowledgeBaseId.trim()
          ? Number(defaultKnowledgeBaseId)
          : null
      });
      setSettings(payload);
      setApiToken("");
      setDefaultKnowledgeBaseId(payload?.default_knowledge_base_id ? String(payload.default_knowledge_base_id) : "");
      onClose();
    } catch (requestError) {
      setError(requestError.message || "保存 Doco 设置失败。");
    } finally {
      setSaving(false);
    }
  }

  async function handleRemove() {
    setRemoving(true);
    setError("");
    try {
      await deleteDocoSettings();
      onClose();
    } catch (requestError) {
      setError(requestError.message || "删除 Doco 设置失败。");
    } finally {
      setRemoving(false);
    }
  }

  return (
    <div
      className="doco-settings-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <form className="doco-settings-dialog" onSubmit={handleSave} onMouseDown={(event) => event.stopPropagation()}>
        <div className="doco-settings-head">
          <div>
            <h2>Doco 知识库</h2>
            <p>配置当前账号自己的 Doco Token 和默认知识库。</p>
          </div>
          <button type="button" className="icon-button" aria-label="关闭 Doco 设置" onClick={onClose}>
            ×
          </button>
        </div>

        {loading ? <p className="doco-settings-state">加载中…</p> : null}
        <div className="auth-field">
          <label className="auth-label" htmlFor="doco-api-token">
            Doco Token
          </label>
          <input
            id="doco-api-token"
            className="auth-input"
            type="password"
            autoComplete="off"
            placeholder={settings?.configured ? "已配置；如需更换请输入新的 Token" : "粘贴你的 Doco Token"}
            value={apiToken}
            onChange={(event) => setApiToken(event.target.value)}
            disabled={loading || saving || removing}
          />
        </div>
        <div className="auth-field">
          <label className="auth-label" htmlFor="doco-default-knowledge-base">
            默认知识库 ID（可选）
          </label>
          <input
            id="doco-default-knowledge-base"
            className="auth-input"
            type="number"
            min="1"
            step="1"
            placeholder="不填则每次由对话指定"
            value={defaultKnowledgeBaseId}
            onChange={(event) => setDefaultKnowledgeBaseId(event.target.value)}
            disabled={loading || saving || removing}
          />
        </div>
        <p className="doco-settings-note">Token 不会回显到页面或对话中；保存后可让助手列出你账号可用的知识库。</p>
        {error ? <p className="doco-settings-error">{error}</p> : null}
        <div className="doco-settings-actions">
          {settings?.configured ? (
            <button type="button" className="button button-secondary" onClick={handleRemove} disabled={loading || saving || removing}>
              {removing ? "删除中…" : "清除配置"}
            </button>
          ) : null}
          <button type="button" className="button button-secondary" onClick={onClose} disabled={saving || removing}>
            取消
          </button>
          <button type="submit" className="button button-primary" disabled={loading || saving || removing}>
            {saving ? "保存中…" : "保存设置"}
          </button>
        </div>
      </form>
    </div>
  );
}
