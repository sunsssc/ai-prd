import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { PrismLight as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import css from "react-syntax-highlighter/dist/esm/languages/prism/css";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import markup from "react-syntax-highlighter/dist/esm/languages/prism/markup";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import yaml from "react-syntax-highlighter/dist/esm/languages/prism/yaml";
import DocumentDetailPanel from "../components/common/DocumentDetailPanel";
import EditIcon from "../components/common/EditIcon";
import MessageCard from "../components/common/MessageCard";
import ReferenceIcon from "../components/common/ReferenceIcon";
import TreeWorkspaceLayout from "../components/layout/TreeWorkspaceLayout";
import {
  commitSkillZip,
  createSkill,
  createSkillEntry,
  deleteSkillEntry,
  deleteSkillFolder,
  discardSkillZip,
  getSkillFile,
  getLatestSkillZipPreview,
  getSkillZipPreview,
  getSkillTree,
  moveSkillEntry,
  previewSkillZip,
  resolveSkillZipGenerationAfterStreamError,
  streamCreateSkill,
  streamGenerateSkillZipMarkdown,
  updateSkillFile
} from "../services/workspaceApi";
import { buildStableContextKey } from "../utils/contextKeys";
import { clearSkillCreateDraft, readSkillCreateDraft, saveSkillCreateDraft } from "../utils/skillCreateDraft";
import { collectFolderIds, findFirstFile, findNodeById, updateNodeById } from "../utils/tree";

SyntaxHighlighter.registerLanguage("bash", bash);
SyntaxHighlighter.registerLanguage("css", css);
SyntaxHighlighter.registerLanguage("html", markup);
SyntaxHighlighter.registerLanguage("javascript", javascript);
SyntaxHighlighter.registerLanguage("json", json);
SyntaxHighlighter.registerLanguage("python", python);
SyntaxHighlighter.registerLanguage("yaml", yaml);

const draftSkillFolderId = "__new-skill-folder__";
const draftSkillFileId = "__new-skill-file__";

const extensionLanguageMap = {
  css: "css",
  html: "html",
  js: "javascript",
  json: "json",
  py: "python",
  sh: "bash",
  yaml: "yaml",
  yml: "yaml"
};

function createEmptySkillForm() {
  return {
    mode: "intent",
    intent: "",
    name: "",
    description: "",
    content: buildManualSkillContent("", ""),
    files: []
  };
}

function createEmptyFileDraft() {
  return {
    editingIndex: null,
    path: "references/example.md",
    content: ""
  };
}

function normalizeSkillZipPreview(payload) {
  const generationStatus = payload.generation_status || "not_started";
  const generatedContent = payload.generated_skill_md_content || "";
  return {
    ...payload,
    folderName: payload.suggested_folder_name,
    skillMdMode: generationStatus === "running"
      ? "generating"
      : generationStatus === "completed" && generatedContent.trim()
      ? "generated"
      : payload.has_skill_md ? "existing" : "pending",
    skillMdContent: generationStatus === "completed" && generatedContent.trim()
      ? generatedContent
      : payload.skill_md_content || ""
  };
}

function mergePolledSkillZipPreview(current, payload) {
  const next = normalizeSkillZipPreview(payload);
  if (!current || next.generation_status === "completed") {
    return next;
  }
  return {
    ...next,
    folderName: current.folderName,
    skillMdMode: next.generation_status === "running" ? "generating" : current.skillMdMode,
    skillMdContent: current.skillMdContent
  };
}

function summarizeSkillZipGenerationDetail(value, fallback) {
  const normalized = String(value || fallback || "")
    .replace(/\s+/g, " ")
    .trim();
  return normalized.length > 140 ? `${normalized.slice(0, 140)}…` : normalized;
}

function getSkillZipGenerationNotice(zipPreview, zipGenerating, zipGenerationStatus) {
  if (zipGenerating || zipPreview.generation_status === "running") {
    return {
      tone: "running",
      title: "AI 正在读取上传内容",
      detail: summarizeSkillZipGenerationDetail(
        zipGenerationStatus || zipPreview.generation_message,
        "正在分析 ZIP 内容并生成 SKILL.md 草案。"
      )
    };
  }
  if (zipPreview.skillMdMode === "generated" || zipPreview.generation_status === "completed") {
    return { tone: "generated", title: "AI 已生成 SKILL.md", detail: "生成结果已显示在下方，可编辑确认后再导入。" };
  }
  if (zipPreview.skillMdMode === "manual") {
    return { tone: "manual", title: "正在手动补充 SKILL.md", detail: "填写完整后即可确认导入。" };
  }
  if (zipPreview.generation_status === "failed") {
    return {
      tone: "failed",
      title: "AI 生成失败",
      detail: zipPreview.generation_error || zipPreview.generation_message || "请重新生成或手动补充。"
    };
  }
  if (zipPreview.generation_status === "interrupted") {
    return {
      tone: "interrupted",
      title: "AI 生成已中断",
      detail: zipPreview.generation_message || "页面刷新或连接关闭中断了生成，请重新生成。"
    };
  }
  if (zipPreview.generation_status === "unknown") {
    return {
      tone: "unknown",
      title: "上次 AI 生成结果未记录",
      detail: "这是状态持久化前启动的任务，无法确认已完成还是失败，请重新生成。"
    };
  }
  return { tone: "pending", title: "压缩包缺少根目录 SKILL.md", detail: "请选择 AI 自动生成，或手动补充后再导入。" };
}

function buildManualSkillContent(name, description) {
  return [
    `# ${name || "新 Skill"}`,
    "",
    "## 使用场景",
    description || "说明这个 Skill 适合处理什么任务。",
    "",
    "## 输入要求",
    "- ",
    "",
    "## 工作流程",
    "1. ",
    "",
    "## 输出格式",
    "- "
  ].join("\n");
}

function getFileLanguage(path = "") {
  const extension = path.split(".").pop()?.toLowerCase() || "";
  return extensionLanguageMap[extension] || "text";
}

function formatFileSize(size = 0) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function getNodeParentPath(path = "") {
  return path.split("/").slice(0, -1).join("/");
}

function getSkillCreatorLabel(node) {
  if (!node || node.ownership_status !== "owned") {
    return "所有者未知（历史 Skill）";
  }
  return node.creator_name || node.creator_email || "未知用户";
}

function SkillTreeActionIcon({ kind }) {
  const commonProps = {
    viewBox: "0 0 16 16",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: "1.35",
    strokeLinecap: "round",
    strokeLinejoin: "round",
    className: "skill-tree-action-icon",
    "aria-hidden": "true"
  };

  if (kind === "delete") {
    return (
      <svg {...commonProps}>
        <path d="m4.5 4.5 7 7M11.5 4.5l-7 7" />
      </svg>
    );
  }

  return kind === "folder" ? (
    <svg {...commonProps}>
      <path d="M1.5 4.75c0-.69.56-1.25 1.25-1.25H6l1.45 1.65h5.8c.69 0 1.25.56 1.25 1.25V12c0 .69-.56 1.25-1.25 1.25H2.75c-.69 0-1.25-.56-1.25-1.25Z" />
      <path d="M8 7.25v3.75M6.13 9.13h3.75" />
    </svg>
  ) : (
    <svg {...commonProps}>
      <path d="M3 1.75h6.5l3.5 3.5v8.25c0 .41-.34.75-.75.75h-8.5A.75.75 0 0 1 3 13.5Z" />
      <path d="M9.5 1.75v3.5H13M8 7.5v4M6 9.5h4" />
    </svg>
  );
}

export function createSkillRunState() {
  return {
    status: "idle",
    markdown: "",
    activity: "",
    bullets: [],
    error: "",
    sessionId: ""
  };
}

function appendSkillRunBullet(bullets, nextBullet) {
  if (!nextBullet || bullets[bullets.length - 1] === nextBullet) {
    return bullets;
  }
  return bullets.concat(nextBullet).slice(-6);
}

function stripSkillDraftProtocol(markdown) {
  return markdown.replace(/<!--\s*AI_PRD_SKILL_DRAFT[\s\S]*?-->/g, "").trim();
}

function buildSkillRunMessage(runState) {
  if (!runState || runState.status === "idle") {
    return null;
  }

  if (runState.status === "failed") {
    return {
      messageId: "skill-create-run",
      role: "assistant",
      time: "刚刚",
      status: "失败",
      title: "Skill 创建失败",
      detail: runState.error || "执行过程中出现错误。",
      bullets: runState.bullets
    };
  }

  return {
    messageId: "skill-create-run",
    role: "assistant",
    time: "刚刚",
    status: runState.status === "completed" ? "完成" : "生成中",
    markdown: stripSkillDraftProtocol(runState.markdown),
    placeholder: runState.markdown ? "正在整理创建结果..." : runState.activity || "正在启动 Skill 创建 agent...",
    isStreaming: runState.status === "running",
    bullets: runState.bullets
  };
}

function addDraftSkillToTree(root) {
  if (!root || findNodeById(root, draftSkillFileId)) {
    return root;
  }

  const draftFolder = {
    id: draftSkillFolderId,
    kind: "folder",
    name: "未保存 Skill",
    path: "workspace/.claude/skills/未保存 Skill",
    children: [
      {
        id: draftSkillFileId,
        kind: "file",
        name: "SKILL.md",
        path: "workspace/.claude/skills/未保存 Skill/SKILL.md",
        children: [],
        has_children: false,
        children_loaded: true,
        content_type: "markdown",
        is_virtual: true
      }
    ],
    has_children: true,
    children_loaded: true,
    child_count: 1,
    is_virtual: true
  };

  return {
    ...root,
    children: [draftFolder, ...(root.children || [])],
    has_children: true,
    child_count: (root.child_count || root.children?.length || 0) + 1
  };
}

function removeDraftSkillFromTree(root) {
  if (!root) {
    return root;
  }

  return {
    ...root,
    children: (root.children || []).filter((child) => child.id !== draftSkillFolderId),
    child_count: Math.max(((root.child_count || root.children?.length || 1) - 1), 0)
  };
}

function treeContainsNode(node, nodeId) {
  if (!node || !nodeId) {
    return false;
  }
  if (node.id === nodeId) {
    return true;
  }
  return (node.children || []).some((child) => treeContainsNode(child, nodeId));
}

function findTopLevelSkillFolderId(root, nodeId) {
  if (!root || !nodeId) {
    return "";
  }
  for (const child of root.children || []) {
    if (treeContainsNode(child, nodeId)) {
      return child.id;
    }
  }
  return "";
}

function findAddedTopLevelSkillFolder(beforeRoot, afterRoot) {
  if (!afterRoot) {
    return null;
  }

  const beforeIds = new Set((beforeRoot?.children || []).map((child) => child.id));
  return (afterRoot.children || []).find((child) => child.kind === "folder" && !beforeIds.has(child.id)) || null;
}

function isIntentCreateRunning(runState) {
  return runState?.status === "running";
}

export default function SkillPage({
  selectedSkillId,
  createAction,
  createMode,
  currentUser,
  onReferenceNode,
  onNavigateAssistant,
  onNavigateSkill,
  createRunState,
  onCreateRunStateChange,
  unreadSkillFolderIds,
  onUnreadSkillFolderIdsChange
}) {
  const restoredCreateDraftRef = useRef(undefined);
  if (restoredCreateDraftRef.current === undefined) {
    restoredCreateDraftRef.current = readSkillCreateDraft(currentUser);
  }
  const restoredCreateDraft = restoredCreateDraftRef.current;
  const shouldRecoverLatestImportRef = useRef(createAction === "import" && !restoredCreateDraft?.zipPreview);
  const [treeRoot, setTreeRoot] = useState(null);
  const [expandedIds, setExpandedIds] = useState(new Set());
  const [currentSkillId, setCurrentSkillId] = useState("");
  const [editingSkillId, setEditingSkillId] = useState(null);
  const [drafts, setDrafts] = useState({});
  const [treeLoading, setTreeLoading] = useState(true);
  const [treeError, setTreeError] = useState("");
  const [selectedFile, setSelectedFile] = useState(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState("");
  const [saving, setSaving] = useState(false);
  const [creating, setCreating] = useState(false);
  const [deletingSkillId, setDeletingSkillId] = useState("");
  const [pendingDeleteSkill, setPendingDeleteSkill] = useState(null);
  const [pendingDeleteEntry, setPendingDeleteEntry] = useState(null);
  const [adminOverrideDialog, setAdminOverrideDialog] = useState(null);
  const [editingAdminOverrideConfirmed, setEditingAdminOverrideConfirmed] = useState(false);
  const [createForm, setCreateForm] = useState(() => restoredCreateDraft?.createForm || createEmptySkillForm());
  const [createError, setCreateError] = useState("");
  const [createAgentOutput, setCreateAgentOutput] = useState("");
  const [createRunDetailVisible, setCreateRunDetailVisible] = useState(false);
  const [fileDialogOpen, setFileDialogOpen] = useState(false);
  const [fileDraft, setFileDraft] = useState(createEmptyFileDraft);
  const [zipPreview, setZipPreview] = useState(() => restoredCreateDraft?.zipPreview || null);
  const [zipLoading, setZipLoading] = useState(false);
  const [zipDiscarding, setZipDiscarding] = useState(false);
  const [zipCancelConfirmVisible, setZipCancelConfirmVisible] = useState(false);
  const [zipGenerating, setZipGenerating] = useState(() => restoredCreateDraft?.zipPreview?.generation_status === "running");
  const [zipGenerationStatus, setZipGenerationStatus] = useState(() => restoredCreateDraft?.zipGenerationStatus || "");
  const [zipDragging, setZipDragging] = useState(false);
  const [entryDialog, setEntryDialog] = useState(null);
  const fileInputRef = useRef(null);
  const zipDragDepthRef = useRef(0);
  const zipGenerationRequestRef = useRef(false);
  const zipGenerationAbortControllerRef = useRef(null);
  const canceledZipGenerationTokenRef = useRef("");
  const fileHighlightRef = useRef(null);
  const deferredFileContent = useDeferredValue(fileDraft.content);
  const activeCreateRunState = createRunState || createSkillRunState();
  const setCreateRunState = onCreateRunStateChange || (() => {});
  const activeUnreadSkillFolderIds = unreadSkillFolderIds || new Set();
  const setUnreadSkillFolderIds = onUnreadSkillFolderIdsChange || (() => {});

  async function loadSkillTree(nextSelectedId) {
    setTreeLoading(true);
    setTreeError("");
    setSelectedFile(null);
    setFileError("");

    try {
      const payload = await getSkillTree();
      const firstFile = findFirstFile(payload.root);
      const initialSelectedId =
        (nextSelectedId && findNodeById(payload.root, nextSelectedId)?.id) ||
        (selectedSkillId && findNodeById(payload.root, selectedSkillId)?.id) ||
        payload.default_file_path ||
        firstFile?.id ||
        payload.root.id;

      setTreeRoot(payload.root);
      setExpandedIds(new Set(collectFolderIds(payload.root)));
      setCurrentSkillId(initialSelectedId);
    } catch (error) {
      setTreeRoot(null);
      setExpandedIds(new Set());
      setCurrentSkillId("");
      setTreeError(error.message);
    } finally {
      setTreeLoading(false);
    }
  }

  useEffect(() => {
    let disposed = false;

    async function loadTree() {
      setTreeLoading(true);
      setTreeError("");
      setSelectedFile(null);
      setFileError("");

      try {
        const payload = await getSkillTree();
        if (disposed) {
          return;
        }

        const firstFile = findFirstFile(payload.root);
        const initialSelectedId =
          (selectedSkillId && findNodeById(payload.root, selectedSkillId)?.id) ||
          payload.default_file_path ||
          firstFile?.id ||
          payload.root.id;

        setTreeRoot(payload.root);
        setExpandedIds(new Set(collectFolderIds(payload.root)));
        setCurrentSkillId(initialSelectedId);
      } catch (error) {
        if (!disposed) {
          setTreeRoot(null);
          setExpandedIds(new Set());
          setCurrentSkillId("");
          setTreeError(error.message);
        }
      } finally {
        if (!disposed) {
          setTreeLoading(false);
        }
      }
    }

    loadTree();
    return () => {
      disposed = true;
    };
  }, [selectedSkillId]);

  useEffect(() => {
    if (!treeRoot || !selectedSkillId) {
      return;
    }

    const matchedNode = findNodeById(treeRoot, selectedSkillId);
    if (matchedNode?.id) {
      setCurrentSkillId(matchedNode.id);
    }
  }, [selectedSkillId, treeRoot]);

  useEffect(() => {
    if (!treeRoot || (createAction !== "new" && createAction !== "import")) {
      return;
    }

    const nextMode = createAction === "import" ? "zip" : createMode === "manual" ? "manual" : "intent";
    setCreateForm((current) => ({ ...current, mode: nextMode }));
    setTreeRoot((current) => addDraftSkillToTree(current));
    setExpandedIds((current) => new Set([...current, treeRoot.id, draftSkillFolderId]));
    setCurrentSkillId(draftSkillFileId);
  }, [createAction, createMode, treeRoot]);

  useEffect(() => {
    if (createAction !== "new" && createAction !== "import") {
      return;
    }
    saveSkillCreateDraft(currentUser, {
      createForm,
      zipPreview,
      zipGenerationStatus
    });
  }, [createAction, createForm, currentUser, zipGenerating, zipGenerationStatus, zipPreview]);

  useEffect(() => {
    if (createAction !== "import" || zipPreview || !shouldRecoverLatestImportRef.current) {
      return;
    }
    shouldRecoverLatestImportRef.current = false;
    let disposed = false;

    async function recoverLatestImport() {
      try {
        const payload = await getLatestSkillZipPreview();
        if (!disposed && payload.preview) {
          setZipPreview(normalizeSkillZipPreview(payload.preview));
          setCreateError("");
        }
      } catch (error) {
        if (!disposed) {
          setCreateError(error.message);
        }
      }
    }

    recoverLatestImport();
    return () => {
      disposed = true;
    };
  }, [createAction, zipPreview]);

  useEffect(() => {
    if (!zipPreview?.token || zipPreview.generation_status !== "running") {
      return;
    }
    let disposed = false;

    async function refreshGenerationStatus() {
      try {
        const payload = await getSkillZipPreview(zipPreview.token);
        if (disposed) return;
        setZipPreview((current) => mergePolledSkillZipPreview(current, payload));
        setZipGenerationStatus(summarizeSkillZipGenerationDetail(payload.generation_message, "正在分析 ZIP 内容..."));
        setZipGenerating(payload.generation_status === "running");
      } catch (error) {
        if (!disposed) {
          setCreateError(`无法刷新生成状态：${error.message}`);
        }
      }
    }

    refreshGenerationStatus();
    const timerId = window.setInterval(refreshGenerationStatus, 1500);
    return () => {
      disposed = true;
      window.clearInterval(timerId);
    };
  }, [zipPreview?.generation_status, zipPreview?.token]);

  useEffect(() => {
    if (!zipPreview?.token || zipPreview.generation_status) {
      return;
    }
    let disposed = false;

    async function hydrateLegacyGenerationStatus() {
      try {
        const payload = await getSkillZipPreview(zipPreview.token);
        if (!disposed) {
          setZipPreview((current) => mergePolledSkillZipPreview(current, payload));
        }
      } catch (error) {
        if (!disposed) {
          setCreateError(`无法确认上次生成状态：${error.message}`);
        }
      }
    }

    hydrateLegacyGenerationStatus();
    return () => {
      disposed = true;
    };
  }, [zipPreview?.generation_status, zipPreview?.token]);

  const selectedNode = useMemo(() => {
    if (!treeRoot) {
      return null;
    }
    return findNodeById(treeRoot, currentSkillId) || treeRoot;
  }, [currentSkillId, treeRoot]);

  const isCreatingSelectedSkill = selectedNode?.id === draftSkillFileId;

  useEffect(() => {
    let disposed = false;

    async function loadFile() {
      if (!selectedNode || selectedNode.kind !== "file" || selectedNode.is_virtual) {
        setSelectedFile(null);
        setFileLoading(false);
        setFileError("");
        return;
      }

      setFileLoading(true);
      setFileError("");
      setSelectedFile(null);
      try {
        const payload = await getSkillFile(selectedNode.path);
        if (!disposed) {
          setSelectedFile(payload);
          setDrafts((current) => ({
            ...current,
            [selectedNode.id]: current[selectedNode.id] ?? payload.content
          }));
        }
      } catch (error) {
        if (!disposed) {
          setSelectedFile(null);
          setFileError(error.message);
        }
      } finally {
        if (!disposed) {
          setFileLoading(false);
        }
      }
    }

    loadFile();
    return () => {
      disposed = true;
    };
  }, [selectedNode]);

  const renderedContent = selectedNode?.kind === "file"
    ? editingSkillId === selectedNode.id
      ? drafts[selectedNode.id] ?? selectedFile?.content ?? ""
      : fileLoading
        ? "正在加载 Skill 内容..."
        : fileError || selectedFile?.content || ""
    : "";
  const createRunMessage = buildSkillRunMessage(activeCreateRunState);
  const shouldShowCreateRunDetail = createRunDetailVisible && createRunMessage;
  const shouldShowIntentCreateStatus = isIntentCreateRunning(activeCreateRunState);
  const zipCanCommit = Boolean(
    zipPreview && (
      zipPreview.skillMdMode === "existing" ||
      ((zipPreview.skillMdMode === "manual" || zipPreview.skillMdMode === "generated") && zipPreview.skillMdContent.trim())
    )
  );
  const zipIsGenerating = Boolean(
    zipGenerating || zipPreview?.generation_status === "running" || zipPreview?.skillMdMode === "generating"
  );
  const zipNeedsSkillMarkdown = Boolean(zipPreview && !zipPreview.has_skill_md && !zipCanCommit);
  const zipGenerationNotice = zipPreview && !zipPreview.has_skill_md
    ? getSkillZipGenerationNotice(zipPreview, zipGenerating, zipGenerationStatus)
    : null;

  function toggleFolder(id) {
    setExpandedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  function handleStartCreate() {
    clearSkillCreateDraft(currentUser);
    setCreateForm(createEmptySkillForm());
    setCreateError("");
    setCreateAgentOutput("");
    setCreateRunState(createSkillRunState());
    setCreateRunDetailVisible(false);
    setZipPreview(null);
    setZipGenerating(false);
    setZipGenerationStatus("");
    setZipDragging(false);
    zipDragDepthRef.current = 0;
    onNavigateSkill?.({ action: "new" });
  }

  function handleSelectSkillNode(nodeId) {
    if (nodeId === draftSkillFolderId) {
      setCurrentSkillId(draftSkillFileId);
      return;
    }
    if (nodeId !== draftSkillFileId && (createAction === "new" || createAction === "import")) {
      setTreeRoot((current) => removeDraftSkillFromTree(current));
      onNavigateSkill?.({ skill: nodeId });
    }
    setCurrentSkillId(nodeId);
    setCreateRunDetailVisible(false);
    const unreadFolderId = findTopLevelSkillFolderId(treeRoot, nodeId);
    if (unreadFolderId && activeUnreadSkillFolderIds.has(unreadFolderId)) {
      setUnreadSkillFolderIds((current) => {
        const next = new Set(current);
        next.delete(unreadFolderId);
        return next;
      });
    }
  }

  function isSkillOwnFolder(node) {
    if (!treeRoot || node.kind !== "folder" || node.is_virtual || node.id === treeRoot.id) {
      return false;
    }
    const rootPrefix = `${treeRoot.path}/`;
    if (!node.path.startsWith(rootPrefix)) {
      return false;
    }
    return !node.path.slice(rootPrefix.length).includes("/");
  }

  async function handleConfirmDeleteSkillFolder() {
    if (!pendingDeleteSkill) {
      return;
    }

    setDeletingSkillId(pendingDeleteSkill.id);
    setFileError("");
    try {
      const payload = await deleteSkillFolder(pendingDeleteSkill.path, {
        adminOverrideConfirmed: pendingDeleteSkill.requires_admin_override
      });
      const firstFile = findFirstFile(payload.root);
      const nextSelectedId = payload.default_file_path || firstFile?.id || payload.root.id;
      setTreeRoot(payload.root);
      setExpandedIds(new Set(collectFolderIds(payload.root)));
      setCurrentSkillId(nextSelectedId);
      setEditingSkillId(null);
      setSelectedFile(null);
      setPendingDeleteSkill(null);
    } catch (error) {
      setFileError(error.message);
    } finally {
      setDeletingSkillId("");
    }
  }

  function applyEntryTree(payload, nextSelectedId) {
    setTreeRoot(payload.root);
    setExpandedIds(new Set(collectFolderIds(payload.root)));
    setCurrentSkillId(
      (nextSelectedId && findNodeById(payload.root, nextSelectedId)?.id) ||
      payload.default_file_path ||
      findFirstFile(payload.root)?.id ||
      payload.root.id
    );
    setSelectedFile(null);
    setEditingSkillId(null);
  }

  function isProtectedSkillEntry(node) {
    if (!node || node.is_virtual || node.id === treeRoot?.id) return true;
    const skillFolderId = findTopLevelSkillFolderId(treeRoot, node.id);
    return node.id === skillFolderId || (node.name === "SKILL.md" && getNodeParentPath(node.path) === skillFolderId);
  }

  function requestAdminOverride(node, actionLabel, onConfirm) {
    if (!node?.requires_admin_override) {
      onConfirm();
      return;
    }
    setAdminOverrideDialog({ node, actionLabel, onConfirm });
  }

  function openCreateEntryDialog(kind, parentNode = selectedNode, adminOverrideConfirmed = false) {
    setFileError("");
    const parentPath = parentNode?.kind === "folder" ? parentNode.path : getNodeParentPath(parentNode?.path);
    setEntryDialog({
      mode: "create",
      kind,
      parentPath,
      path: "",
      name: kind === "folder" ? "新文件夹" : "new-file.md",
      content: "",
      adminOverrideConfirmed
    });
  }

  function handleCreateEntryFromTree(node, kind) {
    requestAdminOverride(node, `在「${node.name}」中新增${kind === "folder" ? "文件夹" : "文件"}`, () => {
      handleSelectSkillNode(node.id);
      setExpandedIds((current) => new Set([...current, node.id]));
      openCreateEntryDialog(kind, node, node.requires_admin_override);
    });
  }

  function openRenameEntryDialog() {
    if (!selectedNode || isProtectedSkillEntry(selectedNode)) return;
    requestAdminOverride(selectedNode, `重命名「${selectedNode.name}」`, () => {
      setFileError("");
      setEntryDialog({
        mode: "rename",
        kind: selectedNode.kind,
        parentPath: getNodeParentPath(selectedNode.path),
        path: selectedNode.path,
        name: selectedNode.name,
        content: "",
        adminOverrideConfirmed: selectedNode.requires_admin_override
      });
    });
  }

  async function saveEntryDialog() {
    if (!entryDialog?.name.trim()) return;
    setSaving(true);
    setFileError("");
    try {
      if (entryDialog.mode === "create") {
        const payload = await createSkillEntry({
          parent_path: entryDialog.parentPath,
          name: entryDialog.name,
          kind: entryDialog.kind,
          content: entryDialog.content,
          admin_override_confirmed: entryDialog.adminOverrideConfirmed
        });
        const nextPath = `${entryDialog.parentPath}/${entryDialog.name}`;
        applyEntryTree(payload, nextPath);
      } else {
        const destinationPath = `${entryDialog.parentPath}/${entryDialog.name}`;
        const payload = await moveSkillEntry(entryDialog.path, destinationPath, {
          adminOverrideConfirmed: entryDialog.adminOverrideConfirmed
        });
        applyEntryTree(payload, destinationPath);
      }
      setEntryDialog(null);
    } catch (error) {
      setFileError(error.message);
    } finally {
      setSaving(false);
    }
  }

  async function handleConfirmDeleteEntry() {
    if (!pendingDeleteEntry) return;
    setSaving(true);
    setFileError("");
    try {
      const payload = await deleteSkillEntry(pendingDeleteEntry.path, {
        adminOverrideConfirmed: pendingDeleteEntry.requires_admin_override
      });
      applyEntryTree(payload, getNodeParentPath(pendingDeleteEntry.path));
      setPendingDeleteEntry(null);
    } catch (error) {
      setFileError(error.message);
    } finally {
      setSaving(false);
    }
  }

  async function handleCancelCreate() {
    if (zipPreview?.token) {
      setZipDiscarding(true);
      setCreateError("");
      try {
        await discardSkillZip(zipPreview.token);
      } catch (error) {
        setCreateError(`取消导入失败：${error.message}`);
        return;
      } finally {
        setZipDiscarding(false);
      }
    }
    clearSkillCreateDraft(currentUser);
    setCreateForm(createEmptySkillForm());
    setCreateError("");
    setCreateAgentOutput("");
    setCreateRunState(createSkillRunState());
    setCreateRunDetailVisible(false);
    setFileDialogOpen(false);
    setZipPreview(null);
    setZipGenerating(false);
    setZipGenerationStatus("");
    setZipDragging(false);
    zipDragDepthRef.current = 0;
    const nextTree = removeDraftSkillFromTree(treeRoot);
    const nextSelectedId = nextTree?.default_file_path || findFirstFile(nextTree)?.id || nextTree?.id || "";
    setTreeRoot(nextTree);
    setCurrentSkillId(nextSelectedId);
    onNavigateSkill?.(nextSelectedId ? { skill: nextSelectedId } : {});
  }

  async function handleCancelZipImport() {
    if (!zipPreview?.token) return;
    setZipDiscarding(true);
    setCreateError("");
    try {
      await discardSkillZip(zipPreview.token);
      setZipPreview(null);
      setZipGenerating(false);
      setZipGenerationStatus("");
      setZipDragging(false);
      zipDragDepthRef.current = 0;
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    } catch (error) {
      setCreateError(`取消导入失败：${error.message}`);
    } finally {
      setZipDiscarding(false);
    }
  }

  async function handleConfirmCancelZipImport() {
    const token = zipPreview?.token;
    setZipCancelConfirmVisible(false);
    if (token && zipIsGenerating) {
      canceledZipGenerationTokenRef.current = token;
      zipGenerationAbortControllerRef.current?.abort();
    }
    await handleCancelZipImport();
  }

  function beginEdit(adminOverrideConfirmed) {
    if (!selectedNode || selectedNode.kind !== "file") {
      return;
    }

    setEditingAdminOverrideConfirmed(adminOverrideConfirmed);
    setEditingSkillId(selectedNode.id);
    setDrafts((current) => ({
      ...current,
      [selectedNode.id]: current[selectedNode.id] ?? selectedFile?.content ?? ""
    }));
  }

  function handleEdit() {
    if (!selectedNode || !selectedNode.can_edit) return;
    requestAdminOverride(selectedNode, `编辑「${selectedNode.name}」`, () => {
      beginEdit(selectedNode.requires_admin_override);
    });
  }

  async function handleSave() {
    if (!selectedNode || selectedNode.kind !== "file") {
      return;
    }

    setSaving(true);
    setFileError("");
    try {
      const payload = await updateSkillFile(selectedNode.path, drafts[selectedNode.id] ?? "", {
        edit_summary: "更新 Skill 内容",
        admin_override_confirmed: editingAdminOverrideConfirmed
      });
      setSelectedFile(payload);
      setTreeRoot((current) =>
        updateNodeById(current, selectedNode.id, (node) => ({
          ...node,
          updated_at: payload.updated_at,
          content_type: payload.content_type
        }))
      );
      setEditingSkillId(null);
      setEditingAdminOverrideConfirmed(false);
    } catch (error) {
      setFileError(error.message);
    } finally {
      setSaving(false);
    }
  }

  async function handleCreateSkill(event) {
    event.preventDefault();
    const isIntentMode = createForm.mode === "intent";
    setCreating(true);
    setCreateError("");
    setCreateAgentOutput("");

    try {
      if (createForm.mode === "zip") {
        if (!zipCanCommit) {
          throw new Error("请先生成或手动补充 SKILL.md。");
        }
        const payload = await commitSkillZip({
          token: zipPreview.token,
          folder_name: zipPreview.folderName,
          skill_md_mode: zipPreview.skillMdMode,
          skill_md_content: zipPreview.skillMdContent
        });
        clearSkillCreateDraft(currentUser);
        setCreateForm(createEmptySkillForm());
        setZipPreview(null);
        setTreeRoot(payload.tree.root);
        setExpandedIds(new Set(collectFolderIds(payload.tree.root)));
        setCurrentSkillId(payload.path);
        onNavigateSkill?.({ skill: payload.path });
        return;
      }

      const skillPayload = {
        name: isIntentMode ? null : createForm.name,
        description: isIntentMode ? "" : createForm.description,
        intent: isIntentMode ? createForm.intent : "",
        content: isIntentMode ? "" : createForm.content,
        files: createForm.files
      };

      if (isIntentMode) {
        let completedTree = null;
        const treeBeforeCreate = removeDraftSkillFromTree(treeRoot);
        const nextSelectedBeforeRun = findFirstFile(treeBeforeCreate)?.id || treeBeforeCreate?.id || "";
        clearSkillCreateDraft(currentUser);
        onNavigateSkill?.(nextSelectedBeforeRun ? { skill: nextSelectedBeforeRun } : {});
        setTreeRoot(treeBeforeCreate);
        setCurrentSkillId(nextSelectedBeforeRun);
        setCreateRunDetailVisible(true);
        setCreateRunState({
          status: "running",
          markdown: "",
          activity: "正在启动 Skill 创建 agent...",
          bullets: ["正在启动 Skill 创建 agent..."],
          error: "",
          sessionId: ""
        });

        await streamCreateSkill({
          payload: skillPayload,
          onEvent: (streamEvent) => {
            if (streamEvent.type === "session") {
              setCreateRunState((current) => ({
                ...current,
                sessionId: streamEvent.session_id || current.sessionId
              }));
              return;
            }

            if (streamEvent.type === "turn_start") {
              setCreateRunState((current) => ({
                ...current,
                status: "running",
                sessionId: streamEvent.session_id || current.sessionId
              }));
              return;
            }

            if (streamEvent.type === "activity") {
              setCreateRunState((current) => ({
                ...current,
                status: "running",
                activity: streamEvent.message || current.activity,
                bullets: appendSkillRunBullet(current.bullets, streamEvent.message)
              }));
              return;
            }

            if (streamEvent.type === "tool_use") {
              const summary = `正在调用工具：${streamEvent.tool_name || "工具"}`;
              setCreateRunState((current) => ({
                ...current,
                status: "running",
                activity: summary,
                bullets: appendSkillRunBullet(current.bullets, summary)
              }));
              return;
            }

            if (streamEvent.type === "skill_use") {
              const summary = `正在使用 Skill：${streamEvent.skill_name || streamEvent.skill_id || "Skill"}`;
              setCreateRunState((current) => ({
                ...current,
                status: "running",
                activity: summary,
                bullets: appendSkillRunBullet(current.bullets, summary)
              }));
              return;
            }

            if (streamEvent.type === "delta") {
              setCreateRunState((current) => ({
                ...current,
                status: "running",
                markdown: `${current.markdown}${streamEvent.delta || ""}`
              }));
              return;
            }

            if (streamEvent.type === "complete") {
              completedTree = streamEvent.tree || null;
              setCreateRunState((current) => ({
                ...current,
                status: "completed",
                sessionId: streamEvent.session_id || current.sessionId,
                activity: "Skill 创建完成。",
                bullets: appendSkillRunBullet(current.bullets, "Skill 创建完成。")
              }));
            }
          }
        });

        setCreateForm(createEmptySkillForm());
        setFileDialogOpen(false);
        if (completedTree?.root) {
          const createdFolder = findAddedTopLevelSkillFolder(treeBeforeCreate, completedTree.root);
          const createdSkillFile = findFirstFile(createdFolder);
          const nextSelectedId =
            createdSkillFile?.id ||
            completedTree.default_file_path ||
            findFirstFile(completedTree.root)?.id ||
            completedTree.root.id;
          const unreadFolderId = createdFolder?.id || findTopLevelSkillFolderId(completedTree.root, nextSelectedId);
          setTreeRoot(completedTree.root);
          setExpandedIds(new Set(collectFolderIds(completedTree.root)));
          setCurrentSkillId(nextSelectedId);
          if (unreadFolderId) {
            setUnreadSkillFolderIds((current) => new Set(current).add(unreadFolderId));
          }
        } else {
          await loadSkillTree();
        }
        setCreateRunDetailVisible(false);
        setCreateRunState(createSkillRunState());
        return;
      }

      const payload = await createSkill(skillPayload);
      clearSkillCreateDraft(currentUser);
      setCreateForm(createEmptySkillForm());
      setFileDialogOpen(false);
      if (payload.mode === "agent") {
        setCreateAgentOutput(payload.agent_output || "");
        if (payload.tree?.root) {
          const nextSelectedId = payload.tree.default_file_path || findFirstFile(payload.tree.root)?.id || payload.tree.root.id;
          setTreeRoot(payload.tree.root);
          setExpandedIds(new Set(collectFolderIds(payload.tree.root)));
          setCurrentSkillId(nextSelectedId);
        } else {
          await loadSkillTree();
        }
      } else {
        await loadSkillTree(payload.path);
      }
      if (payload.path) {
        onNavigateSkill?.({ skill: payload.path });
      }
    } catch (error) {
      setCreateError(error.message);
      if (isIntentMode) {
        setCreateRunDetailVisible(true);
        setCreateRunState((current) => ({
          ...current,
          status: "failed",
          error: error.message
        }));
      }
    } finally {
      setCreating(false);
    }
  }

  function handleCancel() {
    if (!selectedNode || selectedNode.kind !== "file") {
      return;
    }

    setDrafts((current) => ({
      ...current,
      [selectedNode.id]: selectedFile?.content ?? ""
    }));
    setEditingSkillId(null);
    setEditingAdminOverrideConfirmed(false);
  }

  function openFileDialog(index = null) {
    const file = index === null ? createEmptyFileDraft() : createForm.files[index];
    setFileDraft({
      editingIndex: index,
      path: file.path || "references/example.md",
      content: file.content || ""
    });
    setFileDialogOpen(true);
  }

  function saveFileDraft() {
    const nextFile = {
      path: fileDraft.path.trim().replace(/\\/g, "/").replace(/^\/+|\/+$/g, ""),
      content: fileDraft.content
    };
    if (!nextFile.path) {
      return;
    }

    setCreateForm((current) => {
      if (fileDraft.editingIndex === null) {
        return { ...current, files: current.files.concat(nextFile) };
      }
      return {
        ...current,
        files: current.files.map((file, index) => (index === fileDraft.editingIndex ? nextFile : file))
      };
    });
    setFileDialogOpen(false);
  }

  function handleFileEditorScroll(event) {
    if (fileHighlightRef.current) {
      fileHighlightRef.current.scrollTop = event.currentTarget.scrollTop;
      fileHighlightRef.current.scrollLeft = event.currentTarget.scrollLeft;
    }
  }

  async function handleZipFile(file) {
    if (!file) return;
    setZipLoading(true);
    setZipGenerating(false);
    setZipGenerationStatus("");
    setCreateError("");
    try {
      const payload = await previewSkillZip(file);
      setZipPreview(normalizeSkillZipPreview(payload));
    } catch (error) {
      setZipPreview(null);
      setCreateError(error.message);
    } finally {
      setZipLoading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  async function handleGenerateZipSkillMarkdown() {
    if (!zipPreview || zipIsGenerating || zipGenerationRequestRef.current) return;
    const token = zipPreview.token;
    const folderName = zipPreview.folderName.trim();
    if (!folderName) {
      setCreateError("请先填写 Skill 目录名。");
      return;
    }

    zipGenerationRequestRef.current = true;
    canceledZipGenerationTokenRef.current = "";
    const abortController = new AbortController();
    zipGenerationAbortControllerRef.current = abortController;
    setZipGenerating(true);
    setZipGenerationStatus("正在启动 SKILL.md 生成 agent...");
    setCreateError("");
    setZipPreview((current) => current?.token === token ? {
      ...current,
      skillMdMode: "generating",
      generation_status: "running",
      generation_message: "正在启动 SKILL.md 生成 agent...",
      generation_error: ""
    } : current);
    let generationStillRunning = false;
    try {
      let generatedContent = "";
      await streamGenerateSkillZipMarkdown({
        payload: { token, folder_name: folderName },
        signal: abortController.signal,
        onEvent: (streamEvent) => {
          if (streamEvent.type === "session" || streamEvent.type === "turn_start") {
            setZipGenerationStatus("已连接生成 agent，正在分析 ZIP 内容...");
            return;
          }
          if (streamEvent.type === "activity") {
            setZipGenerationStatus(summarizeSkillZipGenerationDetail(streamEvent.message, "正在分析 ZIP 内容..."));
            return;
          }
          if (streamEvent.type === "tool_use") {
            setZipGenerationStatus(`正在调用工具：${streamEvent.tool_name || "工具"}`);
            return;
          }
          if (streamEvent.type === "skill_use") {
            setZipGenerationStatus(`正在使用 Skill：${streamEvent.skill_name || streamEvent.skill_id || "Skill"}`);
            return;
          }
          if (streamEvent.type === "delta") {
            setZipGenerationStatus("正在编写 SKILL.md 草案...");
            return;
          }
          if (streamEvent.type === "complete") {
            generatedContent = streamEvent.content || "";
          }
        }
      });
      if (!generatedContent.trim()) {
        throw new Error("AI 助手未返回完整的 SKILL.md，请重试。");
      }
      setZipPreview((current) => current?.token === token ? {
        ...current,
        skillMdMode: "generated",
        skillMdContent: generatedContent,
        generation_status: "completed",
        generation_message: "SKILL.md 已生成，可编辑后确认导入。",
        generated_skill_md_content: generatedContent,
        generation_error: ""
      } : current);
      setZipGenerationStatus("SKILL.md 已生成，可编辑后确认导入。");
    } catch (error) {
      if (canceledZipGenerationTokenRef.current === token) {
        setZipGenerationStatus("");
        return;
      }
      let persistedPreview = null;
      try {
        persistedPreview = await getSkillZipPreview(token);
      } catch {
        // 持久化状态不可用时，才回退为当前流式请求的错误。
      }

      const resolvedStatus = resolveSkillZipGenerationAfterStreamError(persistedPreview, error.message);
      if (resolvedStatus.status === "completed") {
        setZipPreview((current) => {
          if (current?.token !== token) return current;
          const restored = normalizeSkillZipPreview(persistedPreview);
          return { ...restored, folderName: current.folderName };
        });
        setCreateError("");
        setZipGenerationStatus("SKILL.md 已生成，可编辑后确认导入。");
      } else if (resolvedStatus.status === "running") {
        generationStillRunning = true;
        setZipPreview((current) => mergePolledSkillZipPreview(current, persistedPreview));
        setCreateError("");
        setZipGenerationStatus(summarizeSkillZipGenerationDetail(persistedPreview.generation_message, "正在分析 ZIP 内容..."));
      } else {
        const errorMessage = resolvedStatus.error;
        setCreateError(errorMessage);
        setZipGenerationStatus("");
        setZipPreview((current) => current?.token === token ? {
          ...current,
          skillMdMode: "pending",
          generation_status: "failed",
          generation_message: "SKILL.md 生成失败。",
          generation_error: errorMessage
        } : current);
      }
    } finally {
      zipGenerationRequestRef.current = false;
      if (zipGenerationAbortControllerRef.current === abortController) {
        zipGenerationAbortControllerRef.current = null;
      }
      setZipGenerating(generationStillRunning);
    }
  }

  function handleZipFiles(files) {
    const selectedFiles = Array.from(files || []);
    if (!selectedFiles.length) return;
    if (selectedFiles.length > 1) {
      setZipPreview(null);
      setCreateError("一次只能导入一个 ZIP 文件。");
      return;
    }
    if (!selectedFiles[0].name.toLowerCase().endsWith(".zip")) {
      setZipPreview(null);
      setCreateError("只支持导入 ZIP 文件。");
      return;
    }
    handleZipFile(selectedFiles[0]);
  }

  function handleZipDragEnter(event) {
    event.preventDefault();
    event.stopPropagation();
    if (zipLoading || zipGenerating) return;
    zipDragDepthRef.current += 1;
    if (Array.from(event.dataTransfer?.types || []).includes("Files")) {
      setZipDragging(true);
    }
  }

  function handleZipDragOver(event) {
    event.preventDefault();
    event.stopPropagation();
    if (event.dataTransfer && !zipLoading && !zipGenerating) {
      event.dataTransfer.dropEffect = "copy";
    }
  }

  function handleZipDragLeave(event) {
    event.preventDefault();
    event.stopPropagation();
    zipDragDepthRef.current = Math.max(0, zipDragDepthRef.current - 1);
    if (zipDragDepthRef.current === 0) {
      setZipDragging(false);
    }
  }

  function handleZipDrop(event) {
    event.preventDefault();
    event.stopPropagation();
    zipDragDepthRef.current = 0;
    setZipDragging(false);
    if (!zipLoading && !zipGenerating) {
      handleZipFiles(event.dataTransfer?.files);
    }
  }

  if (treeLoading) {
    return <div className="folder-empty">正在加载 Skill 目录...</div>;
  }

  if (treeError || !treeRoot || !selectedNode) {
    return <div className="folder-empty">{treeError || "Skill 目录为空。"}</div>;
  }

  return (
    <TreeWorkspaceLayout
      className="workspace-shell-skill"
      treeRoot={treeRoot}
      expandedIds={expandedIds}
      onToggleTreeNode={toggleFolder}
      onSelectTreeNode={handleSelectSkillNode}
      selectedNodeId={currentSkillId}
      renderTreeNodeIndicator={(node) =>
        activeUnreadSkillFolderIds.has(node.id) ? <span className="tree-unread-dot" aria-label="未读" title="未读" /> : null
      }
      renderTreeNodeActions={(node) => {
        if (node.id === treeRoot.id) {
          return (
            <button
              type="button"
              className="tree-row-icon-button"
              aria-label="新建 Skill"
              title={shouldShowIntentCreateStatus ? "Skill 创建进行中" : "新建 Skill"}
              disabled={shouldShowIntentCreateStatus}
              onClick={handleStartCreate}
            >
              +
            </button>
          );
        }

        if (node.kind !== "folder" || node.is_virtual || !node.can_edit) {
          return null;
        }

        return (
          <>
            <button
              type="button"
              className="tree-row-icon-button"
              aria-label={`在 ${node.name} 中新建文件`}
              title="新建文件"
              onClick={() => handleCreateEntryFromTree(node, "file")}
            >
              <SkillTreeActionIcon kind="file" />
            </button>
            <button
              type="button"
              className="tree-row-icon-button"
              aria-label={`在 ${node.name} 中新建文件夹`}
              title="新建文件夹"
              onClick={() => handleCreateEntryFromTree(node, "folder")}
            >
              <SkillTreeActionIcon kind="folder" />
            </button>
            {isSkillOwnFolder(node) && node.can_delete ? (
              <button
                type="button"
                className="tree-row-icon-button"
                aria-label={`删除 Skill ${node.name}`}
                title="删除 Skill"
                disabled={deletingSkillId === node.id}
                onClick={() => setPendingDeleteSkill(node)}
              >
                <SkillTreeActionIcon kind="delete" />
              </button>
            ) : null}
          </>
        );
      }}
      paneActions={
        shouldShowIntentCreateStatus ? (
          <button
            type="button"
            className="skill-create-status-button"
            onClick={() => setCreateRunDetailVisible(true)}
            aria-label="查看正在通过意向创建 skill 的进度"
            title="查看创建详情"
          >
            <span className="skill-create-spinner" aria-hidden="true" />
            <span>正在通过意向创建 skill</span>
          </button>
        ) : null
      }
    >
      {isCreatingSelectedSkill ? (
        <section className="skill-draft-panel">
          <form className="skill-draft-form" onSubmit={handleCreateSkill}>
            <div className="skill-draft-head">
              <div>
                <h2 className="skill-draft-title">新建 Skill</h2>
                <p className="skill-draft-subtitle">未保存的 Skill 会先显示在左侧目录中，创建后写入真实文件。</p>
              </div>
              <div className="content-actions skill-draft-actions">
                <button type="button" className="button button-secondary" onClick={handleCancelCreate} disabled={creating || zipGenerating || zipDiscarding || zipNeedsSkillMarkdown}>
                  {zipDiscarding ? "取消中..." : "取消"}
                </button>
                <button type="submit" className="button button-primary" disabled={creating || zipGenerating || zipDiscarding || (createForm.mode === "zip" && !zipCanCommit)}>
                  {creating ? (createForm.mode === "zip" ? "导入中..." : "创建中...") : createForm.mode === "zip" ? "确认导入" : "创建"}
                </button>
              </div>
            </div>

            <div className="skill-create-mode" role="tablist" aria-label="Skill 创建方式">
              <button
                type="button"
                role="tab"
                aria-selected={createForm.mode === "intent"}
                className={`skill-mode-button ${createForm.mode === "intent" ? "skill-mode-button-active" : ""}`}
                disabled={zipNeedsSkillMarkdown || zipDiscarding}
                onClick={() => {
                  setCreateForm((current) => ({ ...current, mode: "intent" }));
                  onNavigateSkill?.({ action: "new" });
                }}
              >
                意向生成
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={createForm.mode === "manual"}
                className={`skill-mode-button ${createForm.mode === "manual" ? "skill-mode-button-active" : ""}`}
                disabled={zipNeedsSkillMarkdown || zipDiscarding}
                onClick={() => {
                  setCreateForm((current) => ({ ...current, mode: "manual" }));
                  onNavigateSkill?.({ action: "new", mode: "manual" });
                }}
              >
                手动编写
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={createForm.mode === "zip"}
                className={`skill-mode-button ${createForm.mode === "zip" ? "skill-mode-button-active" : ""}`}
                disabled={zipNeedsSkillMarkdown || zipDiscarding}
                onClick={() => {
                  setCreateForm((current) => ({ ...current, mode: "zip" }));
                  onNavigateSkill?.({ action: "import" });
                }}
              >
                ZIP 导入
              </button>
            </div>

            {createForm.mode === "intent" ? (
              <label className="skill-field skill-intent-field">
                <span>意向</span>
                <textarea
                  className="skill-intent-editor"
                  value={createForm.intent}
                  onChange={(event) => setCreateForm((current) => ({ ...current, intent: event.target.value }))}
                  placeholder="写一段话说明这个 Skill 要解决什么问题、适合哪些输入、期望怎样输出。"
                  required
                />
              </label>
            ) : createForm.mode === "manual" ? (
              <>
                <div className="skill-form-grid">
                  <label className="skill-field">
                    <span>名称</span>
                    <input
                      className="auth-input"
                      value={createForm.name}
                      onChange={(event) => setCreateForm((current) => ({ ...current, name: event.target.value }))}
                      required
                    />
                  </label>
                  <label className="skill-field">
                    <span>描述</span>
                    <input
                      className="auth-input"
                      value={createForm.description}
                      onChange={(event) => setCreateForm((current) => ({ ...current, description: event.target.value }))}
                    />
                  </label>
                </div>
                <label className="skill-field skill-md-field">
                  <span>SKILL.md</span>
                  <textarea
                    className="skill-md-editor"
                    value={createForm.content}
                    onChange={(event) => setCreateForm((current) => ({ ...current, content: event.target.value }))}
                    required
                  />
                </label>
              </>
            ) : (
              <div className="skill-zip-section">
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".zip,application/zip"
                  hidden
                  onChange={(event) => handleZipFiles(event.target.files)}
                />
                {!zipPreview ? (
                  <button
                    type="button"
                    className={`skill-zip-dropzone ${zipDragging ? "skill-zip-dropzone-active" : ""}`}
                    onClick={() => fileInputRef.current?.click()}
                    onDragEnter={handleZipDragEnter}
                    onDragOver={handleZipDragOver}
                    onDragLeave={handleZipDragLeave}
                    onDrop={handleZipDrop}
                    disabled={zipLoading}
                    aria-live="polite"
                  >
                    <strong>{zipLoading ? "正在解析 ZIP..." : zipDragging ? "松开即可导入" : "选择或拖放 ZIP 文件"}</strong>
                    <span>{zipDragging ? "将使用当前文件触发预览，不会立即写入正式目录。" : "一个 ZIP 对应一个 Skill，最大 20 MB；会先预览，确认后再正式导入。"}</span>
                  </button>
                ) : null}
                {createError ? <div className="skill-create-error">{createError}</div> : null}

                {zipPreview ? (
                  <div className="skill-zip-preview">
                    <div className="skill-zip-identity">
                      <label className="skill-field skill-zip-name-field">
                        <span>导入后的 Skill 名称</span>
                        <input
                          className="auth-input skill-zip-name-input"
                          value={zipPreview.folderName}
                          onChange={(event) => setZipPreview((current) => ({ ...current, folderName: event.target.value }))}
                          required
                        />
                      </label>
                      <div className="skill-zip-summary">
                        <div>
                          <span>来源文件</span>
                          <strong>{zipPreview.archive_name}</strong>
                        </div>
                        <small>{zipPreview.entries.filter((entry) => entry.kind === "file").length} 个文件</small>
                      </div>
                    </div>

                    {!zipPreview.has_skill_md ? (
                      <div className={`skill-zip-warning skill-zip-warning-${zipGenerationNotice.tone}`}>
                        <div>
                          <strong>{zipGenerationNotice.title}</strong>
                          <p>{zipGenerationNotice.detail}</p>
                        </div>
                        <div className="skill-zip-choice">
                          <button
                            type="button"
                            className={`button ${zipIsGenerating || zipPreview.skillMdMode === "generated" ? "button-primary" : "button-secondary"}`}
                            onClick={handleGenerateZipSkillMarkdown}
                            disabled={zipIsGenerating}
                          >
                            {zipIsGenerating ? <span className="skill-create-spinner" aria-hidden="true" /> : null}
                            {zipIsGenerating
                              ? "AI 生成中..."
                              : ["completed", "failed", "interrupted", "unknown"].includes(zipPreview.generation_status)
                                ? "重新生成"
                                : "自动生成"}
                          </button>
                          {!zipIsGenerating
                            && zipPreview.skillMdMode !== "generated"
                            && zipPreview.generation_status !== "completed" ? (
                              <button
                                type="button"
                                className={`button ${zipPreview.skillMdMode === "manual" ? "button-primary" : "button-secondary"}`}
                                onClick={() => setZipPreview((current) => ({ ...current, skillMdMode: "manual" }))}
                              >
                                手动补充
                              </button>
                            ) : null}
                          <button
                            type="button"
                            className="button button-secondary skill-zip-cancel-button"
                            onClick={() => zipIsGenerating ? setZipCancelConfirmVisible(true) : handleCancelZipImport()}
                            disabled={zipDiscarding}
                          >
                            {zipDiscarding ? "取消中..." : "取消本次导入"}
                          </button>
                        </div>
                      </div>
                    ) : (
                      <div className="skill-zip-ok">已找到根目录 SKILL.md，可直接导入或在下方调整内容。</div>
                    )}

                    {["existing", "manual", "generated"].includes(zipPreview.skillMdMode) ? (
                      <label className="skill-field skill-md-field">
                        <span>{zipPreview.skillMdMode === "generated" ? "AI 生成的 SKILL.md（可编辑）" : "SKILL.md"}</span>
                        <textarea
                          className="skill-md-editor"
                          value={zipPreview.skillMdContent}
                          onChange={(event) => setZipPreview((current) => ({ ...current, skillMdContent: event.target.value }))}
                          required
                        />
                      </label>
                    ) : null}

                    <div className="skill-zip-tree" aria-label="ZIP 文件清单">
                      {zipPreview.entries.map((entry) => (
                        <div className={`skill-zip-entry skill-zip-entry-${entry.kind}`} key={`${entry.kind}-${entry.path}`}>
                          <span style={{ "--zip-depth": entry.path.split("/").length - 1 }}>{entry.kind === "folder" ? "▸" : "·"}</span>
                          <code>{entry.path}</code>
                          {entry.kind === "file" ? <small>{formatFileSize(entry.size)}</small> : null}
                        </div>
                      ))}
                    </div>
                  </div>
                ) : null}
              </div>
            )}

            {createForm.mode === "manual" ? (
            <div className="skill-script-list">
              <div className="skill-script-list-head">
                <span>包内文件（路径可包含多级目录）</span>
                <button type="button" className="button button-secondary" onClick={() => openFileDialog()}>
                  添加文件
                </button>
              </div>
              {createForm.files.length ? (
                <div className="skill-script-items">
                  {createForm.files.map((file, index) => (
                    <div className="skill-script-item" key={`${file.path}-${index}`}>
                      <button type="button" className="skill-script-name" onClick={() => openFileDialog(index)}>
                        {file.path}
                      </button>
                      <button
                        type="button"
                        className="toolbar-icon-button"
                        aria-label={`移除文件 ${file.path}`}
                        title="移除文件"
                        onClick={() =>
                          setCreateForm((current) => ({
                            ...current,
                            files: current.files.filter((_, itemIndex) => itemIndex !== index)
                          }))
                        }
                      >
                        ×
                      </button>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
            ) : null}

            {createForm.mode !== "zip" && createError ? <div className="skill-create-error">{createError}</div> : null}
          </form>
        </section>
      ) : shouldShowCreateRunDetail ? (
        <section className="skill-agent-output-panel">
          <div className="skill-agent-output-head">
            <h2 className="skill-draft-title">通过意向创建 skill</h2>
            <div className="content-actions">
              {activeCreateRunState.sessionId ? (
                <button
                  type="button"
                  className="button button-secondary"
                  onClick={() => onNavigateAssistant?.({ sessionId: activeCreateRunState.sessionId })}
                >
                  查看助手会话
                </button>
              ) : null}
              <button type="button" className="toolbar-icon-button" aria-label="收起创建详情" title="收起" onClick={() => setCreateRunDetailVisible(false)}>
                ×
              </button>
            </div>
          </div>
          <MessageCard message={createRunMessage} />
        </section>
      ) : (
        <>
          {createAgentOutput ? (
            <section className="skill-agent-output-panel">
              <div className="skill-agent-output-head">
                <h2 className="skill-draft-title">Agent 输出</h2>
                <button type="button" className="toolbar-icon-button" aria-label="关闭 Agent 输出" title="关闭" onClick={() => setCreateAgentOutput("")}>
                  ×
                </button>
              </div>
              <MessageCard
                message={{
                  messageId: "skill-create-agent-output",
                  role: "assistant",
                  time: "刚刚",
                  status: "完成",
                  markdown: createAgentOutput
                }}
              />
            </section>
          ) : null}
          {selectedNode.ownership_status ? (
            <section className={`skill-owner-summary ${selectedNode.requires_admin_override ? "skill-owner-summary-admin" : ""}`.trim()}>
              <div>
                <span>创建人</span>
                <strong>{getSkillCreatorLabel(selectedNode)}</strong>
                {selectedNode.creator_email && selectedNode.creator_name ? <small>{selectedNode.creator_email}</small> : null}
              </div>
              {selectedNode.ownership_status === "unknown" ? <em>历史 Skill</em> : null}
              {selectedNode.requires_admin_override ? <em>管理员操作需确认</em> : null}
            </section>
          ) : null}
          <DocumentDetailPanel
          node={selectedNode}
          fileTypeLabel={selectedNode.kind === "file" ? "Markdown 文件" : ""}
          fileUpdatedAt={selectedNode.kind === "file" ? selectedFile?.updated_at || selectedNode.updated_at || "" : ""}
          fileContent={renderedContent}
          fileContentFormat={selectedNode.kind === "file" ? "markdown" : "plain"}
          inlineFileHeader={selectedNode.kind === "file"}
          markdownHeaderMode="skill"
          editable={selectedNode.kind === "file" && selectedNode.can_edit}
          isEditing={editingSkillId === selectedNode.id}
          onSave={handleSave}
          onCancel={handleCancel}
          onContentChange={(value) =>
            setDrafts((current) => ({
              ...current,
              [selectedNode.id]: value
            }))
          }
          fileHeaderActions={
            selectedNode.id !== treeRoot.id ? (
              <>
                {selectedNode.kind === "file" ? (
                  <>
                    {selectedNode.can_edit ? (
                      <button
                        type="button"
                        className="toolbar-icon-button"
                        aria-label={`编辑 Skill ${selectedNode.name}`}
                        title="编辑当前文件"
                        onClick={handleEdit}
                        disabled={fileLoading || saving || selectedFile?.content_type === "binary"}
                      >
                        <EditIcon />
                      </button>
                    ) : null}
                    <button
                      type="button"
                      className="toolbar-icon-button document-reference-button"
                      aria-label={`引用 Skill ${selectedNode.name}`}
                      title="引用当前文件"
                      onClick={() =>
                        onReferenceNode?.({
                          key: buildStableContextKey("skill", selectedNode.path || selectedNode.id),
                          tone: "olive",
                          label: `引用 Skill: ${selectedNode.name}`,
                          sourceUri: selectedNode.path
                        })
                      }
                    >
                      <ReferenceIcon />
                    </button>
                  </>
                ) : null}
                {!isProtectedSkillEntry(selectedNode) && selectedNode.can_edit ? (
                  <>
                    <button type="button" className="toolbar-icon-button" aria-label={`重命名 ${selectedNode.name}`} title="重命名" onClick={openRenameEntryDialog}>
                      <EditIcon />
                    </button>
                    <button type="button" className="toolbar-icon-button" aria-label={`删除 ${selectedNode.name}`} title="删除" onClick={() => setPendingDeleteEntry(selectedNode)}>
                      ×
                    </button>
                  </>
                ) : null}
              </>
            ) : null
          }
          fileContentLabel={editingSkillId === selectedNode.id ? "编辑草稿" : "Skill 内容"}
          renderFolderChild={(child) => (
            <button
              key={child.id}
              type="button"
              className={`child-card child-card-button child-card-${child.kind === "folder" ? "folder" : "file"}`}
              onClick={() => {
                if (child.kind === "folder" && !expandedIds.has(child.id)) {
                  toggleFolder(child.id);
                }
                handleSelectSkillNode(child.id);
              }}
              aria-label={`打开 ${child.name}`}
            >
              <strong>{child.name}</strong>
              <p>{child.path}</p>
            </button>
          )}
          />
        </>
      )}

      {fileDialogOpen ? (
        <div className="skill-script-dialog-backdrop" role="presentation">
          <section className="skill-script-dialog" role="dialog" aria-modal="true" aria-label="编辑文件">
            <div className="skill-script-dialog-head">
              <div>
                <h2 className="skill-draft-title">包内文件</h2>
                <p className="skill-draft-subtitle">使用相对路径组织多级目录，例如 references/rules.md。</p>
              </div>
              <button type="button" className="toolbar-icon-button" aria-label="关闭文件编辑" onClick={() => setFileDialogOpen(false)}>
                ×
              </button>
            </div>
            <label className="skill-field">
              <span>路径</span>
              <input
                className="auth-input"
                value={fileDraft.path}
                onChange={(event) => setFileDraft((current) => ({ ...current, path: event.target.value }))}
                placeholder="references/rules.md"
              />
            </label>
            <label className="skill-field">
              <span>内容</span>
              <div className="skill-code-live-editor">
                <div className="skill-code-highlight" ref={fileHighlightRef} aria-hidden="true">
                  <SyntaxHighlighter
                    language={getFileLanguage(fileDraft.path)}
                    style={oneLight}
                    customStyle={{ minHeight: 420, margin: 0, padding: 14, background: "transparent", fontSize: "0.88rem", lineHeight: 1.6 }}
                    codeTagProps={{ style: { fontFamily: "var(--font-mono)" } }}
                    wrapLongLines={false}
                  >
                    {deferredFileContent || " "}
                  </SyntaxHighlighter>
                </div>
                <textarea
                  className="skill-script-code-editor"
                  value={fileDraft.content}
                  onChange={(event) => setFileDraft((current) => ({ ...current, content: event.target.value }))}
                  onScroll={handleFileEditorScroll}
                  spellCheck="false"
                />
              </div>
            </label>
            <div className="content-actions skill-script-dialog-actions">
              <button type="button" className="button button-secondary" onClick={() => setFileDialogOpen(false)}>
                取消
              </button>
              <button type="button" className="button button-primary" onClick={saveFileDraft}>
                保存文件
              </button>
            </div>
          </section>
        </div>
      ) : null}

      {entryDialog ? (
        <div className="skill-delete-dialog-backdrop" role="presentation">
          <section className="skill-entry-dialog" role="dialog" aria-modal="true" aria-label={entryDialog.mode === "create" ? "新建条目" : "重命名条目"}>
            <div>
              <h2 className="skill-draft-title">
                {entryDialog.mode === "create" ? `新建${entryDialog.kind === "folder" ? "文件夹" : "文件"}` : "重命名"}
              </h2>
              <p className="skill-draft-subtitle">目标目录：{entryDialog.parentPath}</p>
            </div>
            <label className="skill-field">
              <span>名称</span>
              <input
                className="auth-input"
                value={entryDialog.name}
                onChange={(event) => setEntryDialog((current) => ({ ...current, name: event.target.value }))}
                autoFocus
              />
            </label>
            {entryDialog.mode === "create" && entryDialog.kind === "file" ? (
              <label className="skill-field">
                <span>初始内容</span>
                <textarea
                  className="skill-md-editor skill-entry-content-editor"
                  value={entryDialog.content}
                  onChange={(event) => setEntryDialog((current) => ({ ...current, content: event.target.value }))}
                />
              </label>
            ) : null}
            {fileError ? <div className="skill-create-error">{fileError}</div> : null}
            <div className="content-actions skill-delete-actions">
              <button type="button" className="button button-secondary" onClick={() => setEntryDialog(null)} disabled={saving}>
                取消
              </button>
              <button type="button" className="button button-primary" onClick={saveEntryDialog} disabled={saving || !entryDialog.name.trim()}>
                {saving ? "保存中..." : "保存"}
              </button>
            </div>
          </section>
        </div>
      ) : null}

      {zipCancelConfirmVisible ? (
        <div className="skill-delete-dialog-backdrop" role="presentation">
          <section className="skill-delete-dialog" role="alertdialog" aria-modal="true" aria-label="确认取消本次 ZIP 导入">
            <div>
              <h2 className="skill-draft-title">取消本次导入？</h2>
              <p className="skill-delete-warning">SKILL.md 仍在自动生成。继续取消将停止生成并删除本次 ZIP 导入草稿，此操作无法恢复。</p>
            </div>
            <div className="content-actions skill-delete-actions">
              <button type="button" className="button button-secondary" onClick={() => setZipCancelConfirmVisible(false)} disabled={zipDiscarding}>
                继续生成
              </button>
              <button type="button" className="button button-primary skill-delete-confirm-button" onClick={handleConfirmCancelZipImport} disabled={zipDiscarding}>
                {zipDiscarding ? "取消中..." : "确认取消导入"}
              </button>
            </div>
          </section>
        </div>
      ) : null}

      {adminOverrideDialog ? (
        <div className="skill-delete-dialog-backdrop" role="presentation">
          <section className="skill-delete-dialog skill-admin-override-dialog" role="alertdialog" aria-modal="true" aria-label="确认管理员操作">
            <div>
              <h2 className="skill-draft-title">确认管理员操作</h2>
              <p className="skill-delete-warning">
                你正在{adminOverrideDialog.actionLabel}。该 Skill 的创建人为“{getSkillCreatorLabel(adminOverrideDialog.node)}”，并非你本人。
              </p>
              <p className="skill-admin-override-note">继续后将以管理员身份修改他人创建的 Skill，请确认这是有意操作。</p>
            </div>
            <div className="content-actions skill-delete-actions">
              <button type="button" className="button button-secondary" onClick={() => setAdminOverrideDialog(null)}>
                取消
              </button>
              <button
                type="button"
                className="button button-primary"
                onClick={() => {
                  const onConfirm = adminOverrideDialog.onConfirm;
                  setAdminOverrideDialog(null);
                  onConfirm();
                }}
              >
                确认以管理员身份继续
              </button>
            </div>
          </section>
        </div>
      ) : null}

      {pendingDeleteEntry ? (
        <div className="skill-delete-dialog-backdrop" role="presentation">
          <section className="skill-delete-dialog" role="dialog" aria-modal="true" aria-label="确认删除文件或文件夹">
            <div>
              <h2 className="skill-draft-title">删除{pendingDeleteEntry.kind === "folder" ? "文件夹" : "文件"}</h2>
              <p className="skill-delete-warning">
                将删除「{pendingDeleteEntry.name}」{pendingDeleteEntry.kind === "folder" ? "及其全部内容" : ""}。此操作不可撤销。
              </p>
              {pendingDeleteEntry.requires_admin_override ? (
                <p className="skill-admin-override-note">管理员提示：该 Skill 的创建人为“{getSkillCreatorLabel(pendingDeleteEntry)}”，并非你本人。</p>
              ) : null}
            </div>
            <div className="content-actions skill-delete-actions">
              <button type="button" className="button button-secondary" onClick={() => setPendingDeleteEntry(null)} disabled={saving}>
                取消
              </button>
              <button type="button" className="button button-primary skill-delete-confirm-button" onClick={handleConfirmDeleteEntry} disabled={saving}>
                {saving ? "删除中..." : "确认删除"}
              </button>
            </div>
          </section>
        </div>
      ) : null}

      {pendingDeleteSkill ? (
        <div className="skill-delete-dialog-backdrop" role="presentation">
          <section className="skill-delete-dialog" role="dialog" aria-modal="true" aria-label="确认删除 Skill">
            <div>
              <h2 className="skill-draft-title">删除 Skill</h2>
              <p className="skill-delete-warning">
                将删除「{pendingDeleteSkill.name}」整个文件夹，包括 `SKILL.md` 及其所有多级文件和文件夹。此操作不可撤销。
              </p>
              {pendingDeleteSkill.requires_admin_override ? (
                <p className="skill-admin-override-note">管理员提示：该 Skill 的创建人为“{getSkillCreatorLabel(pendingDeleteSkill)}”，并非你本人。</p>
              ) : null}
            </div>
            <div className="content-actions skill-delete-actions">
              <button
                type="button"
                className="button button-secondary"
                onClick={() => setPendingDeleteSkill(null)}
                disabled={Boolean(deletingSkillId)}
              >
                取消
              </button>
              <button
                type="button"
                className="button button-primary skill-delete-confirm-button"
                onClick={handleConfirmDeleteSkillFolder}
                disabled={Boolean(deletingSkillId)}
              >
                {deletingSkillId ? "删除中..." : "确认删除"}
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </TreeWorkspaceLayout>
  );
}
