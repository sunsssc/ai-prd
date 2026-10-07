export const knowledgeScopes = [
  {
    id: "requirements",
    label: "需求文档",
    tone: "signal",
    rootPath: "workspace/knowledge/requirements",
    routeVisible: true
  },
  {
    id: "business",
    label: "业务文档",
    tone: "sand",
    rootPath: "workspace/knowledge/business-docs",
    routeVisible: true
  },
  {
    id: "code",
    label: "代码",
    tone: "olive",
    rootPath: "workspace/knowledge/code",
    routeVisible: false
  }
];

export const knowledgeScopeMap = Object.fromEntries(knowledgeScopes.map((scope) => [scope.id, scope]));
export const defaultKnowledgeScopeIds = knowledgeScopes.map((scope) => scope.id);
export const visibleKnowledgeScopes = knowledgeScopes.filter((scope) => scope.routeVisible);
export const visibleKnowledgeTypeIds = new Set(visibleKnowledgeScopes.map((scope) => scope.id));

export function getKnowledgeScopeMentionLabel(scope) {
  return scope ? `@${scope.label}` : "";
}

export function getKnowledgeScopeByRootPath(rootPath) {
  const normalizedRootPath = String(rootPath || "").replace(/\\/g, "/").replace(/\/+$/, "");
  return knowledgeScopes.find((scope) => {
    const logicalRootPath = scope.rootPath.replace(/^workspace\//, "");
    return (
      scope.rootPath === normalizedRootPath ||
      logicalRootPath === normalizedRootPath ||
      normalizedRootPath.endsWith(`/${logicalRootPath}`)
    );
  }) || null;
}

export function getKnowledgeScopeByPath(path) {
  const normalized = String(path || "").replace(/\\/g, "/");
  return knowledgeScopes.find(
    (scope) => {
      const logicalRootPath = scope.rootPath.replace(/^workspace\//, "");
      return (
        normalized === scope.rootPath ||
        normalized.startsWith(`${scope.rootPath}/`) ||
        normalized.includes(`/${scope.rootPath}/`) ||
        normalized.endsWith(`/${scope.rootPath}`) ||
        normalized === logicalRootPath ||
        normalized.startsWith(`${logicalRootPath}/`) ||
        normalized.includes(`/${logicalRootPath}/`) ||
        normalized.endsWith(`/${logicalRootPath}`)
      );
    }
  ) || null;
}
