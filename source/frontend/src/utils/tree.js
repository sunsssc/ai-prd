export function findNodeById(node, targetId) {
  if (!node) {
    return null;
  }

  if (node.id === targetId) {
    return node;
  }

  for (const child of node.children || []) {
    const result = findNodeById(child, targetId);
    if (result) {
      return result;
    }
  }

  return null;
}

export function findFirstFile(node) {
  if (!node) {
    return null;
  }

  if (node.kind === "file") {
    return node;
  }

  for (const child of node.children || []) {
    const result = findFirstFile(child);
    if (result) {
      return result;
    }
  }

  return null;
}

export function collectFolderIds(node) {
  if (!node) {
    return [];
  }

  const current = node.kind === "folder" ? [node.id] : [];
  return current.concat(...(node.children || []).map(collectFolderIds));
}

// 只收集根节点 id（展开首层，让一级子节点可见）
export function collectFirstLevelFolderIds(root) {
  if (!root || root.kind !== "folder") {
    return [];
  }
  return [root.id];
}

// 收集从根到 targetId 节点路径上所有 folder 的 id
export function collectAncestorFolderIds(node, targetId) {
  if (!node || node.kind !== "folder") {
    return null;
  }

  if (node.id === targetId) {
    return [node.id];
  }

  for (const child of node.children || []) {
    const path = collectAncestorFolderIds(child, targetId);
    if (path !== null) {
      return [node.id, ...path];
    }
  }

  return null;
}

export function updateNodeById(node, targetId, updater) {
  if (!node) {
    return node;
  }

  if (node.id === targetId) {
    return updater(node);
  }

  if (!node.children?.length) {
    return node;
  }

  return {
    ...node,
    children: node.children.map((child) => updateNodeById(child, targetId, updater))
  };
}
