import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import DocumentDetailPanel from "../components/common/DocumentDetailPanel";
import MarkdownRenderer from "../components/common/MarkdownRenderer";
import PullRequestIcon from "../components/common/PullRequestIcon";
import ReferenceIcon from "../components/common/ReferenceIcon";
import RefreshIcon from "../components/common/RefreshIcon";
import RequirementPrLinks from "../components/common/RequirementPrLinks";
import TreeWorkspaceLayout from "../components/layout/TreeWorkspaceLayout";
import { getKnowledgeScopeByPath, knowledgeScopeMap } from "../data/knowledgeScopes";
import { useKnowledgeIndex } from "../hooks/useKnowledgeIndex";
import {
  buildKnowledgeAssetUrl,
  applyBusinessDocUpdate,
  createRequirementReviewJob,
  getBusinessDocUpdate,
  ignoreBusinessDocUpdate,
  getKnowledgeChildren,
  getKnowledgeFile,
  getRequirementClickUpEditAccess,
  getRequirementReview,
  getRequirementReviewJob,
  listBusinessDocUpdates,
  listRequirementPullRequests,
  listRequirementReviews,
  markRequirementReviewRead,
  peekKnowledgeChildrenCache,
  refreshRequirementSource
} from "../services/workspaceApi";
import { buildAppHref } from "../utils/hashRoute";
import {
  applyKnowledgeIndexCounts,
  buildDirectoryChildCountMap,
  buildKnowledgeSearchTree,
  normalizeKnowledgeSearchText
} from "../utils/knowledgeIndex";
import { buildStableContextKey } from "../utils/contextKeys";
import { findNodeById, updateNodeById } from "../utils/tree";

const ClickUpContentEditor = lazy(() => import("../components/common/ClickUpContentEditor"));

const fileTypeLabelMap = {
  markdown: "Markdown 文件",
  code: "代码文件",
  plain: "文本文件",
  binary: "二进制文件"
};

const REVIEW_VIEW_SWITCH_DELAY = 160;

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

function scheduleIdleTask(callback) {
  if (typeof window === "undefined") {
    return () => {};
  }
  if (window.requestIdleCallback) {
    const idleId = window.requestIdleCallback(callback, { timeout: 3000 });
    return () => window.cancelIdleCallback(idleId);
  }

  const timerId = window.setTimeout(callback, 1200);
  return () => window.clearTimeout(timerId);
}

function toFileContentFormat(contentType) {
  if (contentType === "markdown") return "markdown";
  if (contentType === "code") return "code";
  return "plain";
}

function isReviewReportPath(path = "") {
  const normalized = path.replace(/\\/g, "/");
  return /^\/[^/]+\/knowledge\/__reviews__\//.test(normalized);
}

function toFileContentClassName(contentType, path = "") {
  const classes = [];
  if (contentType === "code") {
    classes.push("code");
  }
  if (contentType === "markdown" && isReviewReportPath(path)) {
    classes.push("review-report-view");
  }
  return classes.join(" ");
}

function isExternalUrl(value = "") {
  return /^(?:[a-z]+:)?\/\//i.test(value) || value.startsWith("data:");
}

function hasUriScheme(value = "") {
  return /^[a-z][a-z0-9+.-]*:/i.test(value);
}

function decodePathSegment(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function stripHrefSuffix(value) {
  const hashIndex = value.indexOf("#");
  const withoutHash = hashIndex >= 0 ? value.slice(0, hashIndex) : value;
  const queryIndex = withoutHash.indexOf("?");
  return queryIndex >= 0 ? withoutHash.slice(0, queryIndex) : withoutHash;
}

function normalizeWorkspacePath(path) {
  const segments = [];
  for (const segment of path.split("/")) {
    if (!segment || segment === ".") {
      continue;
    }
    if (segment === "..") {
      segments.pop();
      continue;
    }
    segments.push(segment);
  }
  return segments.join("/");
}

function resolveRelativePath(baseFilePath, relativePath) {
  const baseSegments = baseFilePath.split("/");
  baseSegments.pop();
  return normalizeWorkspacePath([...baseSegments, ...relativePath.split("/")].join("/"));
}

function resolveKnowledgeRouteTarget(path) {
  const normalized = normalizeWorkspacePath(path.replace(/\\/g, "/"));
  const scope = getKnowledgeScopeByPath(normalized);
  if (!scope || normalized === scope.rootPath) {
    return null;
  }

  return {
    type: scope.id,
    nodeId: normalized
  };
}

function resolveKnowledgeFileHref(baseFilePath, href) {
  if (!baseFilePath || !href || href.startsWith("#") || href.startsWith("/") || isExternalUrl(href) || hasUriScheme(href)) {
    return href;
  }

  const pathPart = stripHrefSuffix(href);
  if (!pathPart) {
    return href;
  }

  const decodedPath = decodePathSegment(pathPart);
  const absoluteTarget = resolveKnowledgeRouteTarget(decodedPath);
  const routeTarget = absoluteTarget || resolveKnowledgeRouteTarget(resolveRelativePath(baseFilePath, decodedPath));

  return routeTarget ? buildAppHref("knowledge", routeTarget) : href;
}

function mergeTreeNode(root, nextNode) {
  if (!root || root.id === nextNode.id) {
    return nextNode;
  }

  return updateNodeById(root, nextNode.id, () => nextNode);
}

function buildAncestorFolderPaths(rootPath, targetPath) {
  if (!rootPath || !targetPath || !targetPath.startsWith(`${rootPath}/`)) {
    return [rootPath].filter(Boolean);
  }

  const relativePath = targetPath.slice(rootPath.length + 1);
  const segments = relativePath.split("/").filter(Boolean);
  const ancestorPaths = [rootPath];

  let currentPath = rootPath;
  for (const segment of segments.slice(0, -1)) {
    currentPath = `${currentPath}/${segment}`;
    ancestorPaths.push(currentPath);
  }

  return ancestorPaths;
}

function getFolderMetaText(node) {
  if (typeof node.child_count === "number") {
    return `${node.child_count} 项`;
  }
  return "";
}

function formatSyncTime(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function buildSyncSummaryText(summary) {
  if (!summary?.synced_at) {
    return "尚未完成同步";
  }

  const lastChangedTime = formatSyncTime(summary.last_changed_at);
  if (!lastChangedTime || !summary.last_changed_count) {
    const syncedTime = formatSyncTime(summary.synced_at);
    return `${syncedTime} 已同步，暂无文件更新`;
  }

  const sampleFiles = summary.last_sample_files || [];
  const sampleLabel = sampleFiles.length ? `，包括 ${sampleFiles.join("、")}${summary.last_changed_count > sampleFiles.length ? " ..." : ""}` : "";
  return `最后更新：${lastChangedTime}，更新 ${summary.last_changed_count} 个文件${sampleLabel}`;
}

function updateSyncSummaryByPath(current, path, summary) {
  if (!path) {
    return current;
  }
  return {
    ...current,
    [path]: summary || null
  };
}

function hasSyncSummaryForPath(summariesByPath, path) {
  return Object.prototype.hasOwnProperty.call(summariesByPath, path);
}

function formatRiskLevel(value) {
  if (value === "high") return "高风险";
  if (value === "low") return "低风险";
  return "中风险";
}

function clearReviewBadge(root, reviewId) {
  if (!root) {
    return root;
  }
  const nextChildren = (root.children || []).map((child) => clearReviewBadge(child, reviewId));
  const badge = root.review_badge;
  const nextBadge = badge?.latest_review_id === reviewId
    ? badge.unread_count > 1
      ? { ...badge, unread_count: badge.unread_count - 1 }
      : null
    : badge;
  return { ...root, children: nextChildren, review_badge: nextBadge };
}

function clearDocUpdateBadge(root, updateId) {
  if (!root) {
    return root;
  }
  const nextChildren = (root.children || []).map((child) => clearDocUpdateBadge(child, updateId));
  const badge = root.doc_update_badge;
  const nextBadge = badge?.latest_update_id === updateId
    ? badge.pending_count > 1
      ? { ...badge, pending_count: badge.pending_count - 1 }
      : null
    : badge;
  return { ...root, children: nextChildren, doc_update_badge: nextBadge };
}

function getParentPath(path = "") {
  const segments = path.split("/").filter(Boolean);
  segments.pop();
  return segments.join("/");
}

function buildReviewReference(review, sourceName) {
  return {
    key: `review:${review.review_id}`,
    tone: "signal",
    label: `需求评审: ${sourceName}`,
    sourceType: "review_file",
    sourceUri: review.path,
    metadata: {
      review_id: review.review_id,
      review_type: review.review_type,
      source_path: review.source_path,
      risk_level: review.risk_level
    }
  };
}

function buildDocUpdateReference(update, sourceName) {
  return {
    key: `doc-update:${update.update_id}`,
    tone: "signal",
    label: `文档更新提案: ${sourceName}`,
    sourceType: "doc_update_file",
    sourceUri: update.path,
    metadata: {
      update_id: update.update_id,
      artifact_type: update.artifact_type,
      repo_full_name: update.repo_full_name,
      pr_number: update.pr_number
    }
  };
}

function buildRequirementReference(node, knowledgeType, sourcePath) {
  return {
    key: buildStableContextKey("knowledge", sourcePath || node.path || node.id),
    tone: knowledgeScopeMap[knowledgeType]?.tone || "olive",
    label: `引用文件: ${node.name}`,
    scopeId: knowledgeType,
    sourceUri: sourcePath
  };
}

function scrollTreeNodeIntoView(nodeId) {
  if (typeof document === "undefined" || !nodeId) {
    return;
  }
  window.requestAnimationFrame(() => {
    const escapedId = window.CSS?.escape ? window.CSS.escape(nodeId) : nodeId.replace(/"/g, '\\"');
    const row = document.querySelector(`[data-tree-node-id="${escapedId}"]`);
    row?.scrollIntoView({ block: "center", inline: "nearest" });
    row?.focus?.({ preventScroll: true });
  });
}

export default function KnowledgePage({ knowledgeType, initialNodeId, initialReviewId = "", initialReviewAction = "", onReferenceNode, onNavigateKnowledgeNode, onRequirementReviewBadgeChange }) {
  const {
    indexPayload: knowledgeIndex,
    loading: knowledgeIndexLoading,
    error: knowledgeIndexError,
    ensureIndex
  } = useKnowledgeIndex();
  const [treeRoot, setTreeRoot] = useState(null);
  const [selectedNodeId, setSelectedNodeId] = useState("");
  const [expandedIds, setExpandedIds] = useState(new Set());
  const [loadingNodeIds, setLoadingNodeIds] = useState(new Set());
  const [treeLoading, setTreeLoading] = useState(true);
  const [treeError, setTreeError] = useState("");
  const [treeSearchQuery, setTreeSearchQuery] = useState("");
  const [selectedFile, setSelectedFile] = useState(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState("");
  const [syncSummariesByPath, setSyncSummariesByPath] = useState({});
  const [reviewPanelOpen, setReviewPanelOpen] = useState(false);
  const [reviewList, setReviewList] = useState([]);
  const [reviewListLoading, setReviewListLoading] = useState(false);
  const [reviewContentMode, setReviewContentMode] = useState("requirement");
  const [reviewViewSwitching, setReviewViewSwitching] = useState(false);
  const [activeReviewId, setActiveReviewId] = useState("");
  const [activeReview, setActiveReview] = useState(null);
  const [reviewLoading, setReviewLoading] = useState(false);
  const [reviewError, setReviewError] = useState("");
  const [reviewActionBusy, setReviewActionBusy] = useState(false);
  const [reviewJob, setReviewJob] = useState(null);
  const [reviewConfirm, setReviewConfirm] = useState(null);
  const [requirementPullRequests, setRequirementPullRequests] = useState([]);
  const [docUpdatePanelOpen, setDocUpdatePanelOpen] = useState(false);
  const [docUpdateList, setDocUpdateList] = useState([]);
  const [activeDocUpdateId, setActiveDocUpdateId] = useState("");
  const [activeDocUpdate, setActiveDocUpdate] = useState(null);
  const [docUpdateLoading, setDocUpdateLoading] = useState(false);
  const [docUpdateError, setDocUpdateError] = useState("");
  const [docUpdateActionBusy, setDocUpdateActionBusy] = useState(false);
  const [referenceMenuOpen, setReferenceMenuOpen] = useState(false);
  const [prPopoverOpen, setPrPopoverOpen] = useState(false);
  const [refreshingLatestVersion, setRefreshingLatestVersion] = useState(false);
  const [clickUpEditing, setClickUpEditing] = useState(false);
  const [clickUpSaveMessage, setClickUpSaveMessage] = useState("");
  const [clickUpEditAccess, setClickUpEditAccess] = useState(null);
  const autoReviewActionConsumedRef = useRef(false);
  const reviewBusy = reviewJob?.status === "pending" || reviewJob?.status === "running";
  const reviewContentVisible = reviewContentMode === "review" && !!activeReview && (reviewPanelOpen || reviewViewSwitching);
  const reviewContentMaskVisible = reviewLoading || reviewViewSwitching || (reviewPanelOpen && reviewBusy && !activeReview);
  const reviewEntryBusy = reviewBusy || reviewListLoading || reviewLoading || reviewViewSwitching;

  useEffect(() => {
    let disposed = false;

    async function loadTree() {
      const cacheWarm = peekKnowledgeChildrenCache(knowledgeType);
      setTreeError("");
      setSelectedFile(null);
      setFileError("");
      setLoadingNodeIds(new Set());
      if (!cacheWarm) {
        setTreeLoading(true);
      }

      try {
        const payload = await getKnowledgeChildren(knowledgeType);
        if (disposed) {
          return;
        }

        const root = payload.root;
        let nextRoot = root;
        let nextSyncSummariesByPath = { [root.id]: payload.sync_summary || null };
        const nextExpandedIds = new Set([root.id]);
        const targetId = initialNodeId || root.id;

        if (initialNodeId) {
          const ancestorPaths = buildAncestorFolderPaths(root.id, initialNodeId);
          for (const folderPath of ancestorPaths.slice(1)) {
            const subtree = await getKnowledgeChildren(knowledgeType, folderPath);
            if (disposed) {
              return;
            }
            nextRoot = mergeTreeNode(nextRoot, subtree.root);
            nextSyncSummariesByPath = updateSyncSummaryByPath(nextSyncSummariesByPath, subtree.root?.id || folderPath, subtree.sync_summary);
            nextExpandedIds.add(folderPath);
          }

          const targetNode = findNodeById(nextRoot, initialNodeId);
          if (targetNode?.kind === "folder" && targetNode.has_children && !targetNode.children_loaded) {
            const subtree = await getKnowledgeChildren(knowledgeType, initialNodeId);
            if (disposed) {
              return;
            }
            nextRoot = mergeTreeNode(nextRoot, subtree.root);
            nextSyncSummariesByPath = updateSyncSummaryByPath(nextSyncSummariesByPath, subtree.root?.id || initialNodeId, subtree.sync_summary);
            nextExpandedIds.add(initialNodeId);
          }
        }

        setTreeRoot(nextRoot);
        if (knowledgeType === "requirements") {
          onRequirementReviewBadgeChange?.(nextRoot?.review_badge || null);
        }
        setSyncSummariesByPath(nextSyncSummariesByPath);
        setExpandedIds(nextExpandedIds);
        setSelectedNodeId(targetId);
      } catch (error) {
        if (!disposed) {
          if (!cacheWarm) {
            setTreeRoot(null);
            setSyncSummariesByPath({});
            setExpandedIds(new Set());
            setLoadingNodeIds(new Set());
            setSelectedNodeId("");
          }
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
  }, [knowledgeType, onRequirementReviewBadgeChange]);

  const normalizedTreeSearchQuery = normalizeKnowledgeSearchText(treeSearchQuery);

  useEffect(() => {
    if (!treeRoot) {
      return undefined;
    }

    const cancelIdleTask = scheduleIdleTask(() => {
      void ensureIndex().catch(() => {});
    });
    return () => {
      cancelIdleTask();
    };
  }, [ensureIndex, treeRoot]);

  useEffect(() => {
    if (!normalizedTreeSearchQuery) {
      return undefined;
    }

    void ensureIndex().catch(() => {});
    return undefined;
  }, [ensureIndex, normalizedTreeSearchQuery]);

  useEffect(() => {
    let disposed = false;

    async function syncInitialSelection() {
      if (!treeRoot || treeLoading) {
        return;
      }

      let nextRoot = treeRoot;
      const nextExpandedIds = new Set(expandedIds);
      const targetId = initialNodeId || treeRoot.id;
      const ancestorPaths = buildAncestorFolderPaths(treeRoot.id, targetId);

      for (const folderPath of ancestorPaths.slice(1)) {
        const currentNode = findNodeById(nextRoot, folderPath);
        if (currentNode && (currentNode.children_loaded || !currentNode.has_children)) {
          nextExpandedIds.add(folderPath);
          continue;
        }

        const subtree = await getKnowledgeChildren(knowledgeType, folderPath);
        if (disposed) {
          return;
        }
        nextRoot = mergeTreeNode(nextRoot, subtree.root);
        setSyncSummariesByPath((current) => updateSyncSummaryByPath(current, subtree.root?.id || folderPath, subtree.sync_summary));
        nextExpandedIds.add(folderPath);
      }

      const targetNode = findNodeById(nextRoot, targetId);
      if (targetNode?.kind === "folder" && targetNode.has_children && !targetNode.children_loaded) {
        const subtree = await getKnowledgeChildren(knowledgeType, targetId);
        if (disposed) {
          return;
        }
        nextRoot = mergeTreeNode(nextRoot, subtree.root);
        setSyncSummariesByPath((current) => updateSyncSummaryByPath(current, subtree.root?.id || targetId, subtree.sync_summary));
        nextExpandedIds.add(targetId);
      }

      if (!disposed) {
        setTreeRoot(nextRoot);
        setExpandedIds(nextExpandedIds);
        setSelectedNodeId(targetId);
      }
    }

    syncInitialSelection();
    return () => {
      disposed = true;
    };
  }, [initialNodeId, knowledgeType, treeLoading, treeRoot]);

  const selectedNode = useMemo(() => {
    if (!treeRoot) {
      return null;
    }
    return findNodeById(treeRoot, selectedNodeId) || treeRoot;
  }, [treeRoot, selectedNodeId]);

  const directoryChildCountMap = useMemo(
    () => buildDirectoryChildCountMap(treeRoot, knowledgeIndex),
    [knowledgeIndex, treeRoot]
  );
  const treeRootWithIndexCounts = useMemo(
    () => applyKnowledgeIndexCounts(treeRoot, directoryChildCountMap),
    [directoryChildCountMap, treeRoot]
  );
  const treeSearchResult = useMemo(
    () => buildKnowledgeSearchTree(treeRootWithIndexCounts, knowledgeIndex, treeSearchQuery),
    [knowledgeIndex, treeRootWithIndexCounts, treeSearchQuery]
  );
  const isTreeSearching = Boolean(normalizedTreeSearchQuery);
  const isKnowledgeSearchIndexPending = isTreeSearching && !knowledgeIndex && !knowledgeIndexError;
  const displayTreeRoot = isTreeSearching ? treeSearchResult.root : treeRootWithIndexCounts;
  const displayExpandedIds = isTreeSearching ? treeSearchResult.expandedIds : expandedIds;
  const treeSearchStatusText = isTreeSearching
    ? knowledgeIndexLoading || isKnowledgeSearchIndexPending
      ? "搜索中..."
      : knowledgeIndexError
        ? "搜索不可用"
        : `${treeSearchResult.total} 个匹配`
    : "";
  const treeSearchEmptyState = isTreeSearching && !treeSearchResult.total
    ? knowledgeIndexLoading || isKnowledgeSearchIndexPending
      ? "正在搜索知识库目录..."
      : knowledgeIndexError || `没有找到“${treeSearchQuery.trim()}”`
    : null;

  useEffect(() => {
    let disposed = false;

    async function loadSelectedFolderSummary() {
      if (!selectedNode || selectedNode.kind !== "folder" || hasSyncSummaryForPath(syncSummariesByPath, selectedNode.path)) {
        return;
      }

      try {
        const payload = await getKnowledgeChildren(knowledgeType, selectedNode.path);
        if (disposed) {
          return;
        }
        setTreeRoot((current) => mergeTreeNode(current, payload.root));
        setSyncSummariesByPath((current) => updateSyncSummaryByPath(current, payload.root?.id || selectedNode.path, payload.sync_summary));
      } catch {
        if (!disposed) {
          setSyncSummariesByPath((current) => updateSyncSummaryByPath(current, selectedNode.path, null));
        }
      }
    }

    loadSelectedFolderSummary();
    return () => {
      disposed = true;
    };
  }, [knowledgeType, selectedNode, syncSummariesByPath]);

  const selectedContentPath = selectedNode?.kind === "file"
    ? selectedNode.path
    : selectedNode?.content_path || null;

  useEffect(() => {
    setClickUpEditing(false);
    setClickUpSaveMessage("");
  }, [selectedContentPath]);
  const canShowReviews = knowledgeType === "requirements" && !!selectedContentPath;
  const canShowDocUpdates = knowledgeType === "business" && !!selectedContentPath;

  const resolveKnowledgeImageSrc = useMemo(() => {
    if (!selectedContentPath) {
      return null;
    }

    return (src) => {
      if (!src || isExternalUrl(src) || src.startsWith("/")) {
        return src;
      }
      const resolvedPath = resolveRelativePath(selectedContentPath, src);
      return buildKnowledgeAssetUrl(knowledgeType, resolvedPath);
    };
  }, [knowledgeType, selectedContentPath]);

  const resolveKnowledgeLinkHref = useMemo(() => {
    if (!selectedContentPath) {
      return null;
    }

    return (href) => resolveKnowledgeFileHref(selectedContentPath, href);
  }, [selectedContentPath]);

  useEffect(() => {
    let disposed = false;

    async function loadFile() {
      const filePath = selectedNode?.kind === "file"
        ? selectedNode.path
        : selectedNode?.content_path || null;

      if (!filePath) {
        setSelectedFile(null);
        setFileLoading(false);
        setFileError("");
        return;
      }

      setFileLoading(true);
      setFileError("");
      setSelectedFile(null);
      try {
        const payload = await getKnowledgeFile(knowledgeType, filePath);
        if (!disposed) {
          setSelectedFile(payload);
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
  }, [knowledgeType, selectedNode]);

  const canCheckClickUpEdit = Boolean(
    !reviewContentVisible
    && knowledgeType === "requirements"
    && selectedContentPath
    && (selectedContentPath.includes("/tasks/") || selectedContentPath.includes("/docs/"))
    && selectedNode
    && (selectedNode.kind === "file" || selectedNode.content_path)
    && (selectedFile?.content_type || selectedNode.content_type) === "markdown"
  );

  useEffect(() => {
    let disposed = false;
    if (!canCheckClickUpEdit) {
      setClickUpEditAccess(null);
      return () => {
        disposed = true;
      };
    }

    setClickUpEditAccess(null);
    getRequirementClickUpEditAccess(selectedContentPath)
      .then((payload) => {
        if (!disposed) {
          setClickUpEditAccess(payload);
        }
      })
      .catch(() => {
        if (!disposed) {
          setClickUpEditAccess({
            path: selectedContentPath,
            can_edit: false,
            reason: "权限检查未完成，当前内容按只读处理，请稍后重试。"
          });
        }
      });

    return () => {
      disposed = true;
    };
  }, [canCheckClickUpEdit, selectedContentPath]);

  useEffect(() => {
    let disposed = false;

    async function loadReviews() {
      setReviewList([]);
      setActiveReviewId("");
      setActiveReview(null);
      setReviewContentMode("requirement");
      setReviewViewSwitching(false);
      setReviewError("");
      setReviewJob(null);
      setReviewConfirm(null);
      if (!canShowReviews) {
        setReviewListLoading(false);
        setReviewPanelOpen(false);
        return;
      }

      setReviewListLoading(true);
      try {
        const payload = await listRequirementReviews(selectedContentPath);
        if (disposed) {
          return;
        }
        const reviews = payload.reviews || [];
        setReviewList(reviews);
        const requestedReview = initialReviewId ? reviews.find((review) => review.review_id === initialReviewId) : null;
        const activeCandidate = requestedReview || reviews[0] || null;
        const shouldAutoOpenReview = initialReviewAction === "open" && !autoReviewActionConsumedRef.current;
        if (shouldAutoOpenReview) {
          autoReviewActionConsumedRef.current = true;
        }
        if (activeCandidate) {
          setActiveReviewId(activeCandidate.review_id);
        }
        setReviewPanelOpen(Boolean((initialReviewId || shouldAutoOpenReview) && activeCandidate));
        if (shouldAutoOpenReview && !activeCandidate) {
          setReviewConfirm({
            title: "生成需求评审",
            body: "当前需求还没有需求评审，是否现在生成一次评审？生成会在后台进行。",
            confirmLabel: "生成评审"
          });
        }
      } catch (error) {
        if (!disposed) {
          setReviewError(error.message);
        }
      } finally {
        if (!disposed) {
          setReviewListLoading(false);
        }
      }
    }

    loadReviews();
    return () => {
      disposed = true;
    };
  }, [canShowReviews, initialReviewAction, initialReviewId, selectedContentPath]);

  useEffect(() => {
    let disposed = false;

    async function loadRequirementPullRequests() {
      setRequirementPullRequests([]);
      if (!canShowReviews) {
        return;
      }
      try {
        const payload = await listRequirementPullRequests(selectedContentPath);
        if (!disposed) {
          setRequirementPullRequests(payload.pull_requests || []);
        }
      } catch (error) {
        if (!disposed) {
          setRequirementPullRequests([]);
        }
      }
    }

    loadRequirementPullRequests();
    return () => {
      disposed = true;
    };
  }, [canShowReviews, selectedContentPath]);

  useEffect(() => {
    let disposed = false;

    async function loadDocUpdates() {
      setDocUpdateList([]);
      setActiveDocUpdateId("");
      setActiveDocUpdate(null);
      setDocUpdateError("");
      if (!canShowDocUpdates) {
        setDocUpdatePanelOpen(false);
        return;
      }

      try {
        const payload = await listBusinessDocUpdates(selectedContentPath);
        if (disposed) {
          return;
        }
        const updates = payload.updates || [];
        setDocUpdateList(updates);
        const activeCandidate = updates.find((update) => update.status === "pending") || updates[0] || null;
        if (activeCandidate) {
          setActiveDocUpdateId(activeCandidate.update_id);
        }
        setDocUpdatePanelOpen(Boolean(activeCandidate?.status === "pending"));
      } catch (error) {
        if (!disposed) {
          setDocUpdateError(error.message);
        }
      }
    }

    loadDocUpdates();
    return () => {
      disposed = true;
    };
  }, [canShowDocUpdates, selectedContentPath]);

  useEffect(() => {
    let disposed = false;

    async function loadReviewDetail() {
      if (!reviewPanelOpen || !activeReviewId) {
        setReviewLoading(false);
        return;
      }
      setReviewLoading(true);
      setReviewError("");
      try {
        const detail = await getRequirementReview(activeReviewId);
        if (!disposed) {
          setActiveReview(detail);
          setReviewContentMode("review");
        }
      } catch (error) {
        if (!disposed) {
          setReviewError(error.message);
          setActiveReview(null);
        }
      } finally {
        if (!disposed) {
          setReviewLoading(false);
        }
      }
    }

    loadReviewDetail();
    return () => {
      disposed = true;
    };
  }, [reviewPanelOpen, activeReviewId]);

  useEffect(() => {
    let disposed = false;

    async function loadDocUpdateDetail() {
      if (!docUpdatePanelOpen || !activeDocUpdateId) {
        setActiveDocUpdate(null);
        return;
      }
      setDocUpdateLoading(true);
      setDocUpdateError("");
      try {
        const detail = await getBusinessDocUpdate(activeDocUpdateId);
        if (!disposed) {
          setActiveDocUpdate(detail);
        }
      } catch (error) {
        if (!disposed) {
          setDocUpdateError(error.message);
          setActiveDocUpdate(null);
        }
      } finally {
        if (!disposed) {
          setDocUpdateLoading(false);
        }
      }
    }

    loadDocUpdateDetail();
    return () => {
      disposed = true;
    };
  }, [docUpdatePanelOpen, activeDocUpdateId]);

  useEffect(() => {
    setReferenceMenuOpen(false);
  }, [selectedContentPath, reviewPanelOpen, activeReviewId, docUpdatePanelOpen, activeDocUpdateId]);

  useEffect(() => {
    if (!referenceMenuOpen) {
      return undefined;
    }

    function handlePointerDown(event) {
      if (event.target?.closest?.(".document-reference-menu")) {
        return;
      }
      setReferenceMenuOpen(false);
    }

    function handleKeyDown(event) {
      if (event.key === "Escape") {
        setReferenceMenuOpen(false);
      }
    }

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [referenceMenuOpen]);

  useEffect(() => {
    setPrPopoverOpen(false);
  }, [selectedContentPath, reviewContentMode, reviewPanelOpen, activeReviewId]);

  useEffect(() => {
    if (!prPopoverOpen) {
      return undefined;
    }

    function handlePointerDown(event) {
      if (event.target?.closest?.(".document-pr-menu")) {
        return;
      }
      setPrPopoverOpen(false);
    }

    function handleKeyDown(event) {
      if (event.key === "Escape") {
        setPrPopoverOpen(false);
      }
    }

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [prPopoverOpen]);

  useEffect(() => {
    if (!reviewJob?.job_id || !reviewBusy || !selectedContentPath) {
      return undefined;
    }

    let disposed = false;
    const timer = window.setInterval(async () => {
      try {
        const job = await getRequirementReviewJob(reviewJob.job_id);
        if (disposed) {
          return;
        }
        setReviewJob(job);
        if (job.status === "completed") {
          const payload = await listRequirementReviews(selectedContentPath);
          if (disposed) {
            return;
          }
          const reviews = payload.reviews || [];
          setReviewList(reviews);
          const completedReview = job.review || reviews.find((review) => review.review_id === job.review_id) || reviews[0] || null;
          if (completedReview) {
            setActiveReviewId(completedReview.review_id);
            setReviewPanelOpen(true);
            onNavigateKnowledgeNode?.(selectedNodeId, { reviewId: completedReview.review_id });
            onRequirementReviewBadgeChange?.({
              type: completedReview.review_type,
              unread_count: (treeRoot?.review_badge?.unread_count || 0) + 1,
              latest_review_id: completedReview.review_id
            });
          }
          setReviewError("");
          window.clearInterval(timer);
        } else if (job.status === "failed") {
          setReviewError(job.error_message || "需求评审生成失败。");
          window.clearInterval(timer);
        }
      } catch (error) {
        if (!disposed) {
          setReviewError(error.message);
          window.clearInterval(timer);
        }
      }
    }, 2500);

    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [onNavigateKnowledgeNode, onRequirementReviewBadgeChange, reviewBusy, reviewJob?.job_id, selectedContentPath, selectedNodeId, treeRoot?.review_badge?.unread_count]);

  async function toggleFolder(id) {
    const node = findNodeById(treeRoot, id);
    if (!node || node.kind !== "folder") {
      return;
    }

    const isExpanded = expandedIds.has(id);
    if (isExpanded) {
      setExpandedIds((current) => {
        const next = new Set(current);
        next.delete(id);
        return next;
      });
      return;
    }

    let canExpand = true;
    if (!node.children_loaded && node.has_children) {
      setLoadingNodeIds((current) => new Set(current).add(id));
      try {
        const payload = await getKnowledgeChildren(knowledgeType, node.path);
        setTreeRoot((current) => mergeTreeNode(current, payload.root));
        setSyncSummariesByPath((current) => updateSyncSummaryByPath(current, payload.root?.id || node.path, payload.sync_summary));
      } catch (error) {
        canExpand = false;
      } finally {
        setLoadingNodeIds((current) => {
          const next = new Set(current);
          next.delete(id);
          return next;
        });
      }
    }

    if (!canExpand) {
      return;
    }

    setExpandedIds((current) => {
      const next = new Set(current);
      next.add(id);
      return next;
    });
  }

  function selectKnowledgeNode(id) {
    setSelectedNodeId(id);
    onNavigateKnowledgeNode?.(id);
  }

  async function selectSearchResultNode(id) {
    if (!treeRoot) {
      return;
    }

    let nextRoot = treeRoot;
    const nextExpandedIds = new Set(expandedIds);
    const ancestorPaths = buildAncestorFolderPaths(treeRoot.id, id);

    try {
      for (const folderPath of ancestorPaths.slice(1)) {
        const currentNode = findNodeById(nextRoot, folderPath);
        if (currentNode && (currentNode.children_loaded || !currentNode.has_children)) {
          nextExpandedIds.add(folderPath);
          continue;
        }

        setLoadingNodeIds((current) => new Set(current).add(folderPath));
        const payload = await getKnowledgeChildren(knowledgeType, folderPath);
        nextRoot = mergeTreeNode(nextRoot, payload.root);
        setSyncSummariesByPath((current) => updateSyncSummaryByPath(current, payload.root?.id || folderPath, payload.sync_summary));
        nextExpandedIds.add(folderPath);
      }

      setTreeRoot(nextRoot);
      setExpandedIds(nextExpandedIds);
      setSelectedNodeId(id);
      onNavigateKnowledgeNode?.(id);
      scrollTreeNodeIntoView(id);
    } catch (error) {
      setTreeError(error.message || "打开搜索结果失败。");
    } finally {
      setLoadingNodeIds((current) => {
        const next = new Set(current);
        for (const folderPath of ancestorPaths.slice(1)) {
          next.delete(folderPath);
        }
        return next;
      });
    }
  }

  function toggleSearchResultNode() {
    // 搜索结果保持全展开，让命中的目录路径始终可见。
  }

  async function loadFolderNode(currentRoot, folderNode) {
    if (!folderNode || folderNode.kind !== "folder" || folderNode.children_loaded || !folderNode.has_children) {
      return currentRoot;
    }

    setLoadingNodeIds((current) => new Set(current).add(folderNode.id));
    try {
      const payload = await getKnowledgeChildren(knowledgeType, folderNode.path);
      setSyncSummariesByPath((current) => updateSyncSummaryByPath(current, payload.root?.id || folderNode.path, payload.sync_summary));
      setExpandedIds((current) => {
        const next = new Set(current);
        next.add(folderNode.id);
        return next;
      });
      return mergeTreeNode(currentRoot, payload.root);
    } finally {
      setLoadingNodeIds((current) => {
        const next = new Set(current);
        next.delete(folderNode.id);
        return next;
      });
    }
  }

  async function collectReviewFilesInFolder(folderId) {
    let nextRoot = treeRoot;
    const files = [];

    async function visit(nodeId) {
      let node = findNodeById(nextRoot, nodeId);
      if (!node) {
        return;
      }

      if (node.kind === "file") {
        if (node.review_badge) {
          files.push(node);
        }
        return;
      }

      nextRoot = await loadFolderNode(nextRoot, node);
      node = findNodeById(nextRoot, nodeId);
      for (const child of node?.children || []) {
        if (!child.review_badge && child.kind === "folder") {
          continue;
        }
        await visit(child.id);
      }
    }

    await visit(folderId);
    return { files, root: nextRoot };
  }

  function focusReviewBadgeFile(nextRoot, targetNode) {
    if (!targetNode) {
      return;
    }
    setTreeRoot(nextRoot);
    setExpandedIds((current) => {
      const next = new Set(current);
      for (const folderPath of buildAncestorFolderPaths(treeRoot.id, targetNode.path)) {
        next.add(folderPath);
      }
      return next;
    });
    selectKnowledgeNode(targetNode.id);
    scrollTreeNodeIntoView(targetNode.id);
  }

  async function handleReviewBadgeClick(node) {
    if (!node.review_badge || !treeRoot) {
      return;
    }

    try {
      if (node.kind === "folder") {
        const { files, root } = await collectReviewFilesInFolder(node.id);
        focusReviewBadgeFile(root, files[0]);
        return;
      }

      const parentPath = getParentPath(node.path);
      const parentNode = findNodeById(treeRoot, parentPath);
      if (!parentNode) {
        selectKnowledgeNode(node.id);
        return;
      }

      const { files, root } = await collectReviewFilesInFolder(parentNode.id);
      const currentIndex = files.findIndex((file) => file.id === node.id);
      const targetNode = currentIndex >= 0 && files.length > 1 ? files[(currentIndex + 1) % files.length] : files[0] || node;
      focusReviewBadgeFile(root, targetNode);
    } catch (error) {
      setTreeError(error.message || "跳转评审标记失败。");
    }
  }

  function handleDocUpdateBadgeClick(node) {
    if (!node.doc_update_badge) {
      return;
    }
    selectKnowledgeNode(node.id);
    if (node.kind === "file") {
      setActiveDocUpdateId(node.doc_update_badge.latest_update_id);
      setDocUpdatePanelOpen(true);
    }
  }

  async function handleMarkReviewRead() {
    if (!activeReviewId) {
      return;
    }
    setReviewActionBusy(true);
    setReviewError("");
    try {
      const detail = await markRequirementReviewRead(activeReviewId);
      const nextRoot = clearReviewBadge(treeRoot, activeReviewId);
      setActiveReview(detail);
      setReviewList((current) => current.map((review) => review.review_id === activeReviewId ? { ...review, is_read: true } : review));
      setTreeRoot(nextRoot);
      onRequirementReviewBadgeChange?.(nextRoot?.review_badge || null);
    } catch (error) {
      setReviewError(error.message);
    } finally {
      setReviewActionBusy(false);
    }
  }

  async function startReviewJob() {
    if (!selectedContentPath || reviewBusy) {
      return;
    }
    setReviewPanelOpen(true);
    setReviewError("");
    setReferenceMenuOpen(false);
    setReviewConfirm(null);
    try {
      const job = await createRequirementReviewJob(selectedContentPath);
      setReviewJob(job);
      if (job.status === "completed" && job.review) {
        const payload = await listRequirementReviews(selectedContentPath);
        setReviewList(payload.reviews || [job.review]);
        setActiveReviewId(job.review.review_id);
        onNavigateKnowledgeNode?.(selectedNodeId, { reviewId: job.review.review_id });
      }
    } catch (error) {
      setReviewError(error.message);
    }
  }

  async function handleApplyDocUpdate() {
    if (!activeDocUpdateId) {
      return;
    }
    setDocUpdateActionBusy(true);
    setDocUpdateError("");
    try {
      const detail = await applyBusinessDocUpdate(activeDocUpdateId);
      setActiveDocUpdate(detail);
      setDocUpdateList((current) => current.map((update) => update.update_id === activeDocUpdateId ? { ...update, status: detail.status } : update));
      setTreeRoot((current) => clearDocUpdateBadge(current, activeDocUpdateId));
      setDocUpdatePanelOpen(false);
    } catch (error) {
      setDocUpdateError(error.message);
    } finally {
      setDocUpdateActionBusy(false);
    }
  }

  async function handleIgnoreDocUpdate() {
    if (!activeDocUpdateId) {
      return;
    }
    setDocUpdateActionBusy(true);
    setDocUpdateError("");
    try {
      const detail = await ignoreBusinessDocUpdate(activeDocUpdateId);
      setActiveDocUpdate(detail);
      setDocUpdateList((current) => current.map((update) => update.update_id === activeDocUpdateId ? { ...update, status: detail.status } : update));
      setTreeRoot((current) => clearDocUpdateBadge(current, activeDocUpdateId));
      setDocUpdatePanelOpen(false);
    } catch (error) {
      setDocUpdateError(error.message);
    } finally {
      setDocUpdateActionBusy(false);
    }
  }

  function requestReviewGeneration() {
    if (reviewEntryBusy) {
      return;
    }
    setReviewConfirm({
      title: reviewList.length ? "重新生成需求评审" : "生成需求评审",
      body: reviewList.length
        ? "确认重新生成一次需求评审吗？生成期间不能再次触发评审，可以继续阅读当前已有结果。"
        : "当前需求还没有需求评审，是否现在生成一次评审？生成会在后台进行。",
      confirmLabel: reviewList.length ? "重新评审" : "生成评审"
    });
  }

  async function handleReviewEntryClick() {
    if (reviewEntryBusy) {
      return;
    }
    if (reviewPanelOpen) {
      setReviewViewSwitching(true);
      setReviewPanelOpen(false);
      setReviewError("");
      window.setTimeout(() => {
        setReviewContentMode("requirement");
        setReviewViewSwitching(false);
        onNavigateKnowledgeNode?.(selectedNodeId);
      }, REVIEW_VIEW_SWITCH_DELAY);
      return;
    }
    if (!reviewList.length) {
      requestReviewGeneration();
      return;
    }
    const nextReviewId = activeReviewId || reviewList[0].review_id;
    setActiveReviewId(nextReviewId);
    setReviewLoading(true);
    setReviewPanelOpen(true);
    onNavigateKnowledgeNode?.(selectedNodeId, { reviewId: nextReviewId });
  }

  function referenceRequirement() {
    onReferenceNode?.(buildRequirementReference(selectedNode, knowledgeType, selectedContentPath || selectedNode.path));
  }

  function referenceReview() {
    if (!activeReview) {
      return;
    }
    onReferenceNode?.(buildReviewReference(activeReview, selectedNode.name));
  }

  function referenceDocUpdate() {
    if (!activeDocUpdate) {
      return;
    }
    onReferenceNode?.(buildDocUpdateReference(activeDocUpdate, selectedNode.name));
  }

  function referenceRequirementAndReview() {
    referenceRequirement();
    referenceReview();
    setReferenceMenuOpen(false);
  }

  function referenceBusinessDocAndUpdate() {
    referenceRequirement();
    referenceDocUpdate();
    setReferenceMenuOpen(false);
  }

  function handleReferenceClick() {
    if (docUpdatePanelOpen && activeDocUpdate) {
      setReferenceMenuOpen((current) => !current);
      return;
    }
    if (reviewPanelOpen && activeReview) {
      setReferenceMenuOpen((current) => !current);
      return;
    }
    referenceRequirement();
  }

  async function handleRefreshLatestVersion() {
    if (refreshingLatestVersion || !selectedNode || !hasFileContent || knowledgeType !== "requirements") {
      return;
    }

    const treeTargetPath = selectedNode.kind === "file" ? getParentPath(selectedNode.path) : selectedNode.path;
    const contentPath = selectedContentPath;
    if (!contentPath) {
      return;
    }

    setRefreshingLatestVersion(true);
    setReferenceMenuOpen(false);
    setFileLoading(Boolean(contentPath));
    setFileError("");

    try {
      await refreshRequirementSource(contentPath);
      const [treeResult, fileResult] = await Promise.allSettled([
        treeTargetPath ? getKnowledgeChildren(knowledgeType, treeTargetPath, { force: true }) : Promise.resolve(null),
        getKnowledgeFile(knowledgeType, contentPath, { force: true })
      ]);
      let refreshError = "";

      if (treeResult.status === "fulfilled" && treeResult.value) {
        const payload = treeResult.value;
        setTreeRoot((current) => mergeTreeNode(current, payload.root));
        setSyncSummariesByPath((current) => updateSyncSummaryByPath(current, payload.root?.id || treeTargetPath, payload.sync_summary));
      } else if (treeResult.status === "rejected") {
        refreshError = treeResult.reason?.message || "拉取最新版本失败。";
      }

      if (fileResult.status === "fulfilled" && fileResult.value) {
        setSelectedFile(fileResult.value);
      } else if (fileResult.status === "rejected") {
        refreshError = refreshError || fileResult.reason?.message || "拉取最新版本失败。";
      }

      if (refreshError) {
        throw new Error(refreshError);
      }
    } catch (error) {
      setFileError(error.message || "拉取最新版本失败。");
    } finally {
      setFileLoading(false);
      setRefreshingLatestVersion(false);
    }
  }

  function handleClickUpContentSaved(payload) {
    setClickUpEditing(false);
    setClickUpSaveMessage(payload.message || "已保存到 ClickUp");
    if (!payload.local_synced || !selectedContentPath) return;
    getKnowledgeFile("requirements", selectedContentPath, { force: true })
      .then((nextFile) => setSelectedFile(nextFile))
      .catch(() => setClickUpSaveMessage("已保存到 ClickUp，但页面刷新失败；请手动拉取最新版本。"));
  }

  if (treeLoading) {
    return <div className="folder-empty">正在加载知识库目录...</div>;
  }

  if (treeError || !treeRoot || !selectedNode) {
    return <div className="folder-empty">{treeError || "知识库目录为空。"}</div>;
  }

  const selectedSyncSummary = selectedNode.kind === "folder" ? syncSummariesByPath[selectedNode.path] : null;
  const subtitle = buildSyncSummaryText(selectedSyncSummary);
  const isFile = selectedNode.kind === "file";
  const isFolderWithContent = selectedNode.kind === "folder" && !!selectedNode.content_path;
  const hasFileContent = isFile || isFolderWithContent;
  const requirementFileContent = hasFileContent
    ? fileLoading
      ? "正在加载文件内容..."
      : fileError || selectedFile?.content || ""
    : "";
  const requirementContentFormat = hasFileContent ? toFileContentFormat(selectedFile?.content_type || selectedNode.content_type) : "plain";
  const requirementContentClassName = hasFileContent
    ? toFileContentClassName(selectedFile?.content_type || selectedNode.content_type, selectedFile?.path || selectedNode.path)
    : "";
  const displayFileContent = reviewContentVisible ? activeReview.content : requirementFileContent;
  const displayContentFormat = reviewContentVisible ? "markdown" : requirementContentFormat;
  const displayContentClassName = reviewContentVisible ? "review-report-view" : requirementContentClassName;
  const displayFileUpdatedAt = reviewContentVisible ? activeReview.created_at || "" : isFile ? selectedFile?.updated_at || selectedNode.updated_at || "" : "";
  const canShowRequirementPrs = canShowReviews && !reviewContentVisible && requirementPullRequests.length > 0;
  const canEditClickUpContent = Boolean(
    !reviewContentVisible
    && knowledgeType === "requirements"
    && hasFileContent
    && displayContentFormat === "markdown"
    && selectedContentPath
    && (selectedContentPath.includes("/tasks/") || selectedContentPath.includes("/docs/"))
  );
  const currentClickUpEditAccess = clickUpEditAccess?.path === selectedContentPath
    ? clickUpEditAccess
    : null;

  const reviewContentHeader = reviewContentVisible ? (
    <div className="review-inline-panel">
      <div className="review-panel-head">
        <div>
          <h3>需求评审</h3>
          <p>{reviewList.length ? `${reviewList.length} 条记录` : "当前需求暂无评审记录"}</p>
        </div>
        <div className="review-panel-actions">
          <button type="button" className="button button-secondary" disabled={reviewEntryBusy} onClick={requestReviewGeneration}>
            {reviewBusy ? "评审中..." : "重新评审"}
          </button>
          {activeReview && !activeReview.is_read ? (
            <button type="button" className="button button-primary" disabled={reviewBusy || reviewLoading || reviewActionBusy} onClick={handleMarkReviewRead}>
              标记已阅
            </button>
          ) : null}
        </div>
      </div>

      {reviewError ? <div className="review-panel-error">{reviewError}</div> : null}
      {reviewBusy ? <div className="review-panel-status">评审中，请稍候；完成前不能再次触发评审。</div> : null}

      {reviewList.length > 1 ? (
        <div className="review-tabs" role="tablist">
          {reviewList.map((review) => (
            <button
              key={review.review_id}
              type="button"
              className={`review-tab ${review.review_id === activeReviewId ? "review-tab-active" : ""}`.trim()}
              disabled={reviewLoading}
              onClick={() => {
                if (review.review_id === activeReviewId) {
                  return;
                }
                setReviewLoading(true);
                setActiveReviewId(review.review_id);
                onNavigateKnowledgeNode?.(selectedNodeId, { reviewId: review.review_id });
              }}
            >
              {formatSyncTime(review.created_at) || review.review_id}
            </button>
          ))}
        </div>
      ) : null}

      <div className="review-meta-grid">
        <span>状态：{activeReview.status}</span>
        <span>问题级别：{formatRiskLevel(activeReview.risk_level)}</span>
        <span>生成：{formatSyncTime(activeReview.created_at) || "-"}</span>
        <span>Hash：{activeReview.source_hash}</span>
      </div>
    </div>
  ) : reviewPanelOpen && reviewError && !reviewContentMaskVisible ? (
    <div className="review-inline-panel">
      <div className="review-panel-error">{reviewError}</div>
    </div>
  ) : null;
  const reviewContentOverlay = reviewContentMaskVisible ? (
    <div className="content-loading-mask" aria-hidden="true" />
  ) : null;
  const renderTreeSearch = (variant) => (
    <div className="tree-search" role="search">
      <label className="sr-only" htmlFor={`knowledge-tree-search-${knowledgeType}-${variant}`}>
        搜索知识库目录
      </label>
      <input
        id={`knowledge-tree-search-${knowledgeType}-${variant}`}
        className="tree-search-input"
        type="search"
        value={treeSearchQuery}
        placeholder="搜索目录或文件"
        autoComplete="off"
        onChange={(event) => setTreeSearchQuery(event.target.value)}
      />
      {treeSearchQuery ? (
        <button
          type="button"
          className="tree-search-clear"
          aria-label="清空搜索"
          title="清空搜索"
          onClick={() => setTreeSearchQuery("")}
        >
          ×
        </button>
      ) : null}
      {treeSearchStatusText ? (
        <span className="tree-search-status" aria-live="polite">
          {treeSearchStatusText}
        </span>
      ) : null}
    </div>
  );

  return (
    <TreeWorkspaceLayout
      className="workspace-shell-knowledge"
      treeRoot={displayTreeRoot}
      expandedIds={displayExpandedIds}
      loadingIds={loadingNodeIds}
      onToggleTreeNode={isTreeSearching ? toggleSearchResultNode : toggleFolder}
      onSelectTreeNode={isTreeSearching ? selectSearchResultNode : selectKnowledgeNode}
      selectedNodeId={selectedNodeId}
      paneActions={renderTreeSearch("pane")}
      treeEmptyState={treeSearchEmptyState}
      renderTreeNodeIndicator={(node) =>
        node.doc_update_badge ? (
          <button
            type="button"
            className="doc-update-badge"
            title="查看待确认的业务文档更新提案"
            aria-label="查看待确认的业务文档更新提案"
            onClick={(event) => {
              event.stopPropagation();
              handleDocUpdateBadgeClick(node);
            }}
          >
            待更新
          </button>
        ) : node.review_badge ? (
          <button
            type="button"
            className="review-badge"
            title={node.kind === "folder" ? "跳到本文件夹内第一个带评审的需求" : "跳到本目录下一条带评审的需求"}
            aria-label={node.kind === "folder" ? "跳到本文件夹内第一个带评审的需求" : "跳到本目录下一条带评审的需求"}
            onClick={(event) => {
              event.stopPropagation();
              void handleReviewBadgeClick(node);
            }}
          >
            评
          </button>
        ) : null
      }
    >
      <div className="knowledge-mobile-search">
        {renderTreeSearch("mobile")}
      </div>
      {clickUpSaveMessage && !clickUpEditing ? (
        <div className="clickup-content-save-message" role="status">{clickUpSaveMessage}</div>
      ) : null}
      {clickUpEditing ? (
        <Suspense fallback={<div className="clickup-content-editor-state">正在加载编辑器…</div>}>
          <ClickUpContentEditor
            path={selectedContentPath}
            resolveImageSrc={resolveKnowledgeImageSrc}
            onCancel={() => setClickUpEditing(false)}
            onSaved={handleClickUpContentSaved}
          />
        </Suspense>
      ) : (
      <DocumentDetailPanel
        node={selectedNode.kind === "folder" ? { ...selectedNode, subtitle } : selectedNode}
        fileTypeLabel={
          isFile
            ? fileTypeLabelMap[(selectedFile?.content_type || selectedNode.content_type)] || "文件"
            : ""
        }
        fileUpdatedAt={displayFileUpdatedAt}
        fileContent={displayFileContent}
        fileContentFormat={displayContentFormat}
        fileContentClassName={displayContentClassName}
        inlineFileHeader={!reviewContentVisible && hasFileContent && (selectedFile?.content_type || selectedNode.content_type) === "markdown"}
        resolveImageSrc={resolveKnowledgeImageSrc}
        resolveLinkHref={resolveKnowledgeLinkHref}
        contentHeader={reviewContentHeader}
        contentOverlay={reviewContentOverlay}
        clickUpCommentsPath={
          !reviewContentVisible
          && knowledgeType === "requirements"
          && isFile
          && selectedNode.path.includes("/tasks/")
            ? selectedNode.path
            : ""
        }
        fileHeaderActions={
          hasFileContent ? (
            <>
              {canEditClickUpContent && currentClickUpEditAccess?.can_edit ? (
                <button
                  type="button"
                  className="button button-primary clickup-content-edit-button"
                  onClick={() => {
                    setClickUpSaveMessage("");
                    setClickUpEditing(true);
                  }}
                >
                  编辑
                </button>
              ) : null}
              {canEditClickUpContent && !currentClickUpEditAccess?.can_edit ? (
                <span
                  className="clickup-content-readonly"
                  role="img"
                  aria-label="权限无关，所以只读"
                  data-tooltip={currentClickUpEditAccess?.reason || "正在检查编辑权限…"}
                  title={currentClickUpEditAccess?.reason || "正在检查编辑权限…"}
                  tabIndex={0}
                >
                  <ReadOnlyDocumentIcon />
                </span>
              ) : null}
              {knowledgeType === "requirements" ? (
                <button
                  type="button"
                  className="toolbar-icon-button document-refresh-button"
                  aria-label="手动拉取最新版本"
                  title={refreshingLatestVersion ? "正在拉取最新版本" : "手动拉取最新版本"}
                  disabled={refreshingLatestVersion}
                  aria-busy={refreshingLatestVersion ? "true" : undefined}
                  onClick={handleRefreshLatestVersion}
                >
                  <RefreshIcon />
                </button>
              ) : null}
              {canShowDocUpdates ? (
                <button
                  type="button"
                  className={`doc-update-entry-button ${docUpdateList.some((update) => update.status === "pending") ? "doc-update-entry-button-pending" : "doc-update-entry-button-empty"}`.trim()}
                  onClick={() => {
                    if (!docUpdateList.length && !docUpdateError) {
                      return;
                    }
                    setDocUpdatePanelOpen((current) => !current);
                    setReviewPanelOpen(false);
                    setReviewContentMode("requirement");
                    setReviewViewSwitching(false);
                  }}
                  disabled={!docUpdateList.length && !docUpdateError}
                  title={docUpdateError
                    ? (docUpdatePanelOpen ? "返回业务文档" : "读取更新提案失败，点击查看错误")
                    : docUpdateList.length
                      ? (docUpdatePanelOpen ? "返回业务文档" : "查看业务文档更新提案")
                      : "暂无业务文档更新提案"}
                >
                  {docUpdateList.some((update) => update.status === "pending") ? "待更新" : docUpdateError ? "更新异常" : "更新记录"}
                </button>
              ) : null}
              {canShowRequirementPrs ? (
                <span className={`document-pr-menu ${prPopoverOpen ? "document-pr-menu-open" : ""}`.trim()}>
                  <button
                    type="button"
                    className={`pr-entry-button ${prPopoverOpen ? "pr-entry-button-open" : ""}`.trim()}
                    aria-label={`查看关联 PR ${requirementPullRequests.length}`}
                    title={prPopoverOpen ? "收起关联 PR" : `查看关联 PR · ${requirementPullRequests.length}`}
                    aria-haspopup="dialog"
                    aria-expanded={prPopoverOpen}
                    onClick={() => setPrPopoverOpen((current) => !current)}
                  >
                    <PullRequestIcon />
                    <span className="pr-entry-count">{requirementPullRequests.length}</span>
                  </button>
                  {prPopoverOpen ? (
                    <div className="pr-popover" role="dialog" aria-label="关联 PR">
                      <RequirementPrLinks pullRequests={requirementPullRequests} />
                    </div>
                  ) : null}
                </span>
              ) : null}
              {canShowReviews ? (
                <button
                  type="button"
                  className={`review-entry-button ${reviewList.length ? "review-entry-button-reviewed" : "review-entry-button-empty"} ${reviewEntryBusy ? "review-entry-button-busy" : ""}`.trim()}
                  onClick={handleReviewEntryClick}
                  disabled={reviewEntryBusy}
                  aria-busy={reviewEntryBusy ? "true" : undefined}
                  title={reviewBusy ? "评审中" : reviewListLoading ? "正在读取评审记录" : reviewLoading ? "正在加载评审详情" : reviewPanelOpen ? "返回需求详情" : reviewList.length ? "查看需求评审" : "生成需求评审"}
                >
                  {reviewEntryBusy ? "" : reviewPanelOpen ? "←" : "评"}
                </button>
              ) : null}
              <span className={`document-reference-menu ${referenceMenuOpen ? "document-reference-menu-open" : ""}`.trim()}>
                <button
                  type="button"
                  className="toolbar-icon-button document-reference-button"
                  aria-label={(reviewPanelOpen && activeReview) || (docUpdatePanelOpen && activeDocUpdate) ? `选择引用方式 ${selectedNode.name}` : `引用文件 ${selectedNode.name}`}
                  title={(reviewPanelOpen && activeReview) || (docUpdatePanelOpen && activeDocUpdate) ? "选择引用方式" : "引用当前文件"}
                  disabled={(reviewPanelOpen && !activeReview) || (docUpdatePanelOpen && !activeDocUpdate)}
                  aria-haspopup={(reviewPanelOpen && activeReview) || (docUpdatePanelOpen && activeDocUpdate) ? "menu" : undefined}
                  aria-expanded={(reviewPanelOpen && activeReview) || (docUpdatePanelOpen && activeDocUpdate) ? referenceMenuOpen : undefined}
                  onClick={handleReferenceClick}
                >
                  <ReferenceIcon />
                </button>
                {docUpdatePanelOpen && activeDocUpdate ? (
                  referenceMenuOpen ? (
                    <div className="reference-menu" role="menu">
                      <button type="button" role="menuitem" onClick={() => {
                        referenceDocUpdate();
                        setReferenceMenuOpen(false);
                      }}>
                        引用提案到对话
                      </button>
                      <button type="button" role="menuitem" onClick={referenceBusinessDocAndUpdate}>
                        引用业务文档 + 提案
                      </button>
                    </div>
                  ) : null
                ) : reviewPanelOpen && activeReview ? (
                  referenceMenuOpen ? (
                    <div className="reference-menu" role="menu">
                      <button type="button" role="menuitem" onClick={() => {
                        referenceReview();
                        setReferenceMenuOpen(false);
                      }}>
                        引用评审到对话
                      </button>
                      <button type="button" role="menuitem" onClick={referenceRequirementAndReview}>
                        引用需求 + 评审
                      </button>
                    </div>
                  ) : null
                ) : null}
              </span>
            </>
          ) : null
        }
        fileContentLabel="文件内容"
        renderFolderChild={(child) => (
          <button
            key={child.id}
            type="button"
            className={`child-card child-card-button child-card-${child.kind === "folder" ? "folder" : "file"}`}
            onClick={() => {
              if (child.kind === "folder" && !expandedIds.has(child.id)) {
                void toggleFolder(child.id);
              }
              selectKnowledgeNode(child.id);
            }}
            aria-label={`打开 ${child.name}`}
          >
            <strong>{child.name}</strong>
            <p>{child.path}</p>
            <p className="muted">{child.kind === "folder" ? getFolderMetaText(child) : formatSyncTime(child.updated_at) || "可查看"}</p>
          </button>
        )}
      />
      )}
      {docUpdatePanelOpen && canShowDocUpdates ? (
        <section className="doc-update-panel">
          <div className="review-panel-head">
            <div>
              <h3>业务文档更新</h3>
              <p>{docUpdateList.length ? `${docUpdateList.length} 条提案` : "当前业务文档暂无更新提案"}</p>
            </div>
            <div className="review-panel-actions">
              {activeDocUpdate?.status === "pending" ? (
                <>
                  <button type="button" className="button button-secondary" disabled={docUpdateActionBusy} onClick={handleIgnoreDocUpdate}>
                    忽略
                  </button>
                  <button type="button" className="button button-primary" disabled={docUpdateActionBusy} onClick={handleApplyDocUpdate}>
                    应用
                  </button>
                </>
              ) : null}
            </div>
          </div>

          {docUpdateError ? <div className="review-panel-error">{docUpdateError}</div> : null}

          {docUpdateList.length > 1 ? (
            <div className="review-tabs" role="tablist">
              {docUpdateList.map((update) => (
                <button
                  key={update.update_id}
                  type="button"
                  className={`review-tab ${update.update_id === activeDocUpdateId ? "review-tab-active" : ""}`.trim()}
                  onClick={() => setActiveDocUpdateId(update.update_id)}
                >
                  {formatSyncTime(update.created_at) || update.update_id}
                </button>
              ))}
            </div>
          ) : null}

          {docUpdateLoading ? (
            <div className="review-panel-empty">正在加载提案...</div>
          ) : activeDocUpdate ? (
            <>
              <div className="review-meta-grid">
                <span>状态：{activeDocUpdate.status}</span>
                <span>来源：{activeDocUpdate.mode === "full" ? "全量校准" : "增量更新"}</span>
                <span>仓库：{activeDocUpdate.repo_key || "多仓库"}</span>
                <span>生成：{formatSyncTime(activeDocUpdate.created_at) || "-"}</span>
              </div>
              <div className="review-meta-grid">
                <span>Base：{activeDocUpdate.base_sha || "-"}</span>
                <span>Head：{activeDocUpdate.head_sha || "-"}</span>
                <span>审核：{activeDocUpdate.review_status || "-"}</span>
                <span>置信度：{activeDocUpdate.confidence == null ? "-" : `${Math.round(activeDocUpdate.confidence * 100)}%`}</span>
              </div>
              <div className="review-content">
                <MarkdownRenderer markdown={activeDocUpdate.content} variant="document" resolveLinkHref={resolveKnowledgeLinkHref} />
              </div>
            </>
          ) : (
            <div className="review-panel-empty">代码校准或增量分析产生异常提案后会在这里等待人工确认。</div>
          )}
        </section>
      ) : null}
      {reviewConfirm ? (
        <div className="review-confirm-backdrop" role="presentation" onClick={() => setReviewConfirm(null)}>
          <div
            className="review-confirm-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="review-confirm-title"
            onClick={(event) => event.stopPropagation()}
          >
            <h3 id="review-confirm-title">{reviewConfirm.title}</h3>
            <p>{reviewConfirm.body}</p>
            <div className="review-confirm-actions">
              <button type="button" className="button button-secondary" onClick={() => setReviewConfirm(null)}>
                取消
              </button>
              <button type="button" className="button button-primary" onClick={startReviewJob}>
                {reviewConfirm.confirmLabel}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </TreeWorkspaceLayout>
  );
}
