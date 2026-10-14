import { useEffect, useRef, useState } from "react";
import TreeView from "../common/TreeView";
import { findNodeById } from "../../utils/tree";

const MOBILE_QUERY = "(max-width: 840px)";

function isMobileViewport() {
  if (typeof window === "undefined" || !window.matchMedia) {
    return false;
  }
  return window.matchMedia(MOBILE_QUERY).matches;
}

export default function TreeWorkspaceLayout({
  className = "",
  treeRoot,
  expandedIds,
  loadingIds,
  onToggleTreeNode,
  onSelectTreeNode,
  selectedNodeId,
  paneActions = null,
  treeEmptyState = null,
  renderTreeNodeActions,
  renderTreeNodeIndicator,
  children
}) {
  const [treeCollapsed, setTreeCollapsed] = useState(isMobileViewport);
  const detailPanelRef = useRef(null);

  useEffect(() => {
    detailPanelRef.current?.scrollTo({ top: 0, left: 0 });
  }, [selectedNodeId]);

  // 移动端「选中文件」后自动收起目录抽屉，避免遮挡内容；
  // 选中文件夹时保持抽屉打开，方便用户继续向下浏览。
  useEffect(() => {
    if (!isMobileViewport() || !treeRoot || !selectedNodeId) {
      return;
    }
    const node = findNodeById(treeRoot, selectedNodeId);
    if (node && node.kind === "file") {
      setTreeCollapsed(true);
    }
  }, [selectedNodeId, treeRoot]);

  return (
    <div className={`workspace-shell ${className} ${treeCollapsed ? "tree-collapsed" : ""}`.trim()}>
      <button
        type="button"
        className="workspace-mobile-tree-toggle"
        aria-label={treeCollapsed ? "打开目录" : "关闭目录"}
        title={treeCollapsed ? "打开目录" : "关闭目录"}
        onClick={() => setTreeCollapsed((current) => !current)}
      >
        <span className="workspace-mobile-tree-toggle-icon" aria-hidden="true">
          {treeCollapsed ? "≡" : "×"}
        </span>
      </button>
      <div
        className="workspace-tree-backdrop"
        onClick={() => setTreeCollapsed(true)}
        aria-hidden="true"
      />
      <aside className="tree-pane">
        <div className="pane-head">
          {paneActions ? <div className="pane-actions">{paneActions}</div> : null}
          <button
            type="button"
            className="toolbar-icon-button pane-toggle-button"
            onClick={() => setTreeCollapsed((current) => !current)}
            aria-label={treeCollapsed ? "展开面板" : "收起面板"}
            title={treeCollapsed ? "展开面板" : "收起面板"}
          >
            <span className="toolbar-icon">{treeCollapsed ? "›" : "‹"}</span>
          </button>
        </div>

        <div className="tree-body">
          <TreeView
            root={treeRoot}
            expandedIds={expandedIds}
            loadingIds={loadingIds}
            onToggle={onToggleTreeNode}
            onSelect={onSelectTreeNode}
            selectedId={selectedNodeId}
            renderNodeActions={renderTreeNodeActions}
            renderNodeIndicator={renderTreeNodeIndicator}
          />
          {treeEmptyState ? <div className="tree-empty-state">{treeEmptyState}</div> : null}
        </div>
      </aside>

      <section ref={detailPanelRef} className="detail-panel">{children}</section>
    </div>
  );
}
