import { getKnowledgeScopeByRootPath, knowledgeScopeMap } from "../data/knowledgeScopes.js";

export function normalizeKnowledgeSearchText(value = "") {
  return String(value).trim().toLocaleLowerCase("zh-CN");
}

function cleanRelativePath(path = "", { trimTrailing = false } = {}) {
  const cleaned = String(path).replace(/^\/+/, "");
  return trimTrailing ? cleaned.replace(/\/+$/, "") : cleaned;
}

function normalizeRootPath(path = "") {
  return String(path).replace(/\\/g, "/").replace(/\/+$/, "");
}

function getPathName(path = "") {
  const segments = String(path).split("/").filter(Boolean);
  return segments[segments.length - 1] || path;
}

function getParentPath(path = "") {
  const value = String(path);
  const segments = value.split("/").filter(Boolean);
  segments.pop();
  const parentPath = segments.join("/");
  return value.startsWith("/") ? `/${parentPath}` : parentPath;
}

function sortTreeNodes(nodes) {
  return [...nodes].sort((first, second) => {
    if (first.kind !== second.kind) {
      return first.kind === "folder" ? -1 : 1;
    }
    return first.name.localeCompare(second.name, "zh-CN", { numeric: true, sensitivity: "base" });
  });
}

export function getKnowledgeIndexScope(indexPayload, rootPath) {
  if (!indexPayload || !rootPath) {
    return null;
  }

  const normalizedRootPath = normalizeRootPath(rootPath);
  const prefix = `${normalizedRootPath}/`;
  const scopeIndex = (indexPayload.prefixes || []).findIndex((item) => item === prefix);
  if (scopeIndex < 0) {
    return null;
  }

  const scope = getKnowledgeScopeByRootPath(normalizedRootPath);
  if (!scope) {
    return null;
  }
  return {
    prefix,
    rootPath: normalizedRootPath,
    scopeIndex,
    scopeId: scope.id,
    scope
  };
}

export function buildKnowledgeFullPath(indexPayload, scopeIndex, relativePath) {
  const prefix = indexPayload?.prefixes?.[scopeIndex];
  const cleanPath = cleanRelativePath(relativePath);
  if (!prefix || !cleanPath) {
    return "";
  }
  return `${prefix}${cleanPath}`;
}

function scopeForPrefix(prefix = "") {
  return getKnowledgeScopeByRootPath(prefix.replace(/\/+$/, ""));
}

export function listKnowledgeFiles(indexPayload, scopeId) {
  if (!indexPayload?.files?.length) {
    return [];
  }

  return indexPayload.files
    .map(([scopeIndex, relativePath]) => {
      if (typeof relativePath !== "string") {
        return null;
      }
      const fullPath = buildKnowledgeFullPath(indexPayload, scopeIndex, relativePath);
      if (!fullPath) {
        return null;
      }
      const scope = scopeForPrefix(indexPayload.prefixes?.[scopeIndex]);
      if (!scope) {
        return null;
      }
      const record = {
        scopeIndex,
        scopeId: scope.id,
        relativePath: cleanRelativePath(relativePath),
        fullPath,
        name: getPathName(relativePath)
      };
      return !scopeId || record.scopeId === scopeId ? record : null;
    })
    .filter(Boolean);
}

export function listKnowledgeDirectories(indexPayload, scopeId) {
  if (!indexPayload?.directories?.length) {
    return [];
  }

  return indexPayload.directories
    .map(([scopeIndex, relativePath, childCount]) => {
      if (typeof relativePath !== "string") {
        return null;
      }
      const cleanPath = cleanRelativePath(relativePath, { trimTrailing: true });
      if (!cleanPath) {
        return null;
      }
      const fullPath = buildKnowledgeFullPath(indexPayload, scopeIndex, cleanPath);
      if (!fullPath) {
        return null;
      }
      const scope = scopeForPrefix(indexPayload.prefixes?.[scopeIndex]);
      if (!scope) {
        return null;
      }
      const record = {
        scopeIndex,
        scopeId: scope.id,
        relativePath: cleanPath,
        fullPath,
        name: getPathName(cleanPath),
        childCount: typeof childCount === "number" ? childCount : undefined
      };
      return !scopeId || record.scopeId === scopeId ? record : null;
    })
    .filter(Boolean);
}

export function matchKnowledgeFiles(indexPayload, query, options = {}) {
  const normalizedQuery = normalizeKnowledgeSearchText(query);
  if (!normalizedQuery) {
    return [];
  }

  const limit = options.limit ?? 8;
  return listKnowledgeFiles(indexPayload, options.scopeId)
    .filter((file) => normalizeKnowledgeSearchText(file.relativePath).includes(normalizedQuery))
    .slice(0, limit)
    .map((file) => ({
      ...file,
      scope: knowledgeScopeMap[file.scopeId] || null
    }));
}

export function buildDirectoryChildCountMap(treeRoot, indexPayload) {
  const scope = getKnowledgeIndexScope(indexPayload, treeRoot?.id);
  const childCountByPath = new Map();
  if (!treeRoot || !scope) {
    return childCountByPath;
  }

  const directChildrenByPath = new Map();
  const explicitChildCountByPath = new Map();

  function appendDirectChild(parentPath, childPath) {
    if (!parentPath || !childPath) {
      return;
    }
    const childSet = directChildrenByPath.get(parentPath) || new Set();
    childSet.add(childPath);
    directChildrenByPath.set(parentPath, childSet);
  }

  for (const directory of listKnowledgeDirectories(indexPayload, scope.scopeId)) {
    const directoryPath = `${treeRoot.id}/${directory.relativePath}`;
    appendDirectChild(getParentPath(directoryPath), directoryPath);
    if (typeof directory.childCount === "number") {
      explicitChildCountByPath.set(directoryPath, directory.childCount);
    }
  }

  for (const file of listKnowledgeFiles(indexPayload, scope.scopeId)) {
    appendDirectChild(getParentPath(file.fullPath), file.fullPath);
  }

  for (const [directoryPath, childSet] of directChildrenByPath) {
    childCountByPath.set(directoryPath, explicitChildCountByPath.get(directoryPath) ?? childSet.size);
  }

  for (const [directoryPath, childCount] of explicitChildCountByPath) {
    childCountByPath.set(directoryPath, childCount);
  }

  return childCountByPath;
}

export function applyKnowledgeIndexCounts(node, childCountByPath) {
  if (!node || !childCountByPath?.size) {
    return node;
  }

  if (node.kind !== "folder") {
    return node;
  }

  const children = (node.children || []).map((child) => applyKnowledgeIndexCounts(child, childCountByPath));
  const indexedChildCount = childCountByPath.get(node.path);
  return {
    ...node,
    children,
    child_count: typeof indexedChildCount === "number" ? indexedChildCount : node.child_count
  };
}

export function buildKnowledgeSearchTree(treeRoot, indexPayload, query) {
  const normalizedQuery = normalizeKnowledgeSearchText(query);
  const emptyResult = {
    root: treeRoot ? { ...treeRoot, children: [], child_count: 0, children_loaded: true } : null,
    expandedIds: new Set(treeRoot ? [treeRoot.id] : []),
    total: 0
  };

  if (!treeRoot || !indexPayload || !normalizedQuery) {
    return emptyResult;
  }

  const scope = getKnowledgeIndexScope(indexPayload, treeRoot.id);
  if (!scope) {
    return emptyResult;
  }

  const folderRecords = new Map([
    [
      treeRoot.id,
      {
        id: treeRoot.id,
        kind: "folder",
        name: treeRoot.name,
        path: treeRoot.path,
        content_path: treeRoot.content_path,
        content_type: treeRoot.content_type
      }
    ]
  ]);

  for (const directory of listKnowledgeDirectories(indexPayload, scope.scopeId)) {
    const segments = directory.relativePath.split("/").filter(Boolean);
    let currentPath = treeRoot.id;
    for (const [segmentIndex, segment] of segments.entries()) {
      currentPath = `${currentPath}/${segment}`;
      const isTargetDirectory = segmentIndex === segments.length - 1;
      if (!folderRecords.has(currentPath)) {
        folderRecords.set(currentPath, {
          id: currentPath,
          kind: "folder",
          name: segment,
          path: currentPath,
          child_count: typeof directory.childCount === "number" && isTargetDirectory ? directory.childCount : undefined
        });
      } else if (typeof directory.childCount === "number" && isTargetDirectory) {
        folderRecords.set(currentPath, { ...folderRecords.get(currentPath), child_count: directory.childCount });
      }
    }
  }

  const fileRecords = [];
  for (const file of listKnowledgeFiles(indexPayload, scope.scopeId)) {
    const segments = file.relativePath.split("/").filter(Boolean);
    let currentPath = treeRoot.id;
    for (const segment of segments.slice(0, -1)) {
      currentPath = `${currentPath}/${segment}`;
      if (!folderRecords.has(currentPath)) {
        folderRecords.set(currentPath, {
          id: currentPath,
          kind: "folder",
          name: segment,
          path: currentPath
        });
      }
    }

    fileRecords.push({
      id: file.fullPath,
      kind: "file",
      name: file.name,
      path: file.fullPath,
      children: [],
      has_children: false,
      children_loaded: true
    });
  }

  const matchedRecords = [];
  for (const folder of folderRecords.values()) {
    if (folder.id === treeRoot.id) {
      continue;
    }
    const haystack = normalizeKnowledgeSearchText(`${folder.name}\n${folder.path}`);
    if (haystack.includes(normalizedQuery)) {
      matchedRecords.push(folder);
    }
  }
  for (const file of fileRecords) {
    const haystack = normalizeKnowledgeSearchText(`${file.name}\n${file.path}`);
    if (haystack.includes(normalizedQuery)) {
      matchedRecords.push(file);
    }
  }

  if (!matchedRecords.length) {
    return emptyResult;
  }

  const treeNodeById = new Map();
  const expandedIds = new Set([treeRoot.id]);
  const rootNode = {
    ...treeRoot,
    children: [],
    children_loaded: true,
    child_count: 0
  };
  treeNodeById.set(treeRoot.id, rootNode);

  function ensureFolder(path) {
    if (!path) {
      return rootNode;
    }
    const existingNode = treeNodeById.get(path);
    if (existingNode) {
      return existingNode;
    }

    const record = folderRecords.get(path) || {
      id: path,
      kind: "folder",
      name: getPathName(path),
      path
    };
    const parentPath = getParentPath(path);
    const parentNode = parentPath && path !== treeRoot.id ? ensureFolder(parentPath) : rootNode;
    const node = {
      ...record,
      children: [],
      has_children: true,
      children_loaded: true,
      child_count: typeof record.child_count === "number" ? record.child_count : 0
    };
    treeNodeById.set(path, node);
    parentNode.children.push(node);
    expandedIds.add(path);
    return node;
  }

  for (const record of matchedRecords) {
    if (record.kind === "folder") {
      ensureFolder(record.path);
      continue;
    }
    const parentNode = ensureFolder(getParentPath(record.path));
    if (!treeNodeById.has(record.path)) {
      const fileNode = { ...record };
      treeNodeById.set(record.path, fileNode);
      parentNode.children.push(fileNode);
    }
  }

  for (const node of treeNodeById.values()) {
    if (node.kind === "folder") {
      node.children = sortTreeNodes(node.children || []);
      node.child_count = typeof node.child_count === "number" ? node.child_count : node.children.length;
      node.has_children = node.children.length > 0;
    }
  }

  return {
    root: rootNode,
    expandedIds,
    total: matchedRecords.length
  };
}

export function getKnowledgeScopeForIndex(indexPayload, scopeIndex) {
  return scopeForPrefix(indexPayload?.prefixes?.[scopeIndex]);
}
