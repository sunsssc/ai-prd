function getNodeMeta(node, isLoading) {
  if (node.kind === "folder") {
    if (isLoading) {
      return "加载中...";
    }
    if (typeof node.child_count === "number") {
      return `${node.child_count} 项`;
    }
    return "";
  }

  return node.status || "";
}

function TreeNode({ node, depth, expandedIds, loadingIds, onToggle, onSelect, selectedId, renderNodeActions, renderNodeIndicator }) {
  const isFolder = node.kind === "folder";
  const isFolderDoc = isFolder && !!node.content_path;
  const isExpanded = expandedIds.has(node.id);
  const isSelected = node.id === selectedId;
  const meta = getNodeMeta(node, loadingIds?.has(node.id));
  const nodeActions = renderNodeActions?.(node);
  const nodeIndicator = renderNodeIndicator?.(node);

  function handleClick() {
    onSelect(node.id);
    if (isFolder) {
      onToggle(node.id);
    }
  }

  function handleKeyDown(event) {
    if (event.key !== "Enter" && event.key !== " ") {
      return;
    }
    event.preventDefault();
    handleClick();
  }

  return (
    <div className={`tree-node ${isFolder ? "tree-node-folder" : "tree-node-file"}`.trim()}>
      <div
        role="button"
        tabIndex={0}
        className={`tree-row ${isSelected ? "tree-row-active" : ""} ${isFolder ? "tree-row-folder" : "tree-row-file"}`.trim()}
        style={{ "--tree-depth": depth }}
        data-tree-node-id={node.id}
        onClick={handleClick}
        onKeyDown={handleKeyDown}
        aria-expanded={isFolder ? isExpanded : undefined}
      >
        <span className={`tree-caret ${isFolder ? "" : "tree-caret-hidden"} ${isExpanded ? "tree-caret-open" : ""}`.trim()}>
          ›
        </span>
        <span className={`tree-glyph ${isFolderDoc ? "tree-glyph-folder-doc" : isFolder ? "tree-glyph-folder" : "tree-glyph-file"}`.trim()} aria-hidden="true" />
        <span className="tree-label">
          <span className="tree-label-text">{node.title || node.name}</span>
        </span>
        {nodeIndicator ? <span className="tree-row-indicator">{nodeIndicator}</span> : null}
        {meta ? <span className="tree-meta">{meta}</span> : null}
        {nodeActions ? (
          <span className="tree-row-actions" onClick={(event) => event.stopPropagation()}>
            {nodeActions}
          </span>
        ) : null}
      </div>

      {isFolder && isExpanded ? (
        <div className="tree-children">
          {(node.children || []).map((child) => (
            <TreeNode
              key={child.id}
              node={child}
              depth={depth + 1}
              expandedIds={expandedIds}
              loadingIds={loadingIds}
              onToggle={onToggle}
              onSelect={onSelect}
              selectedId={selectedId}
              renderNodeActions={renderNodeActions}
              renderNodeIndicator={renderNodeIndicator}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

export default function TreeView({ root, expandedIds, loadingIds = new Set(), onToggle, onSelect, selectedId, renderNodeActions, renderNodeIndicator }) {
  return (
    <div className="tree-view">
      <TreeNode
        node={root}
        depth={0}
        expandedIds={expandedIds}
        loadingIds={loadingIds}
        onToggle={onToggle}
        onSelect={onSelect}
        selectedId={selectedId}
        renderNodeActions={renderNodeActions}
        renderNodeIndicator={renderNodeIndicator}
      />
    </div>
  );
}
