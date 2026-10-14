const DEFAULT_ROUTE = {
  page: "assistant",
  view: "chat",
  preset: "default",
  sessionId: "",
  type: "requirements",
  skill: "",
  skillAction: "",
  skillMode: "",
  nodeId: "",
  reviewId: "",
  reviewAction: "",
  q: "",
  shareToken: ""
};

export function parseHashRoute() {
  const rawHash = window.location.hash.replace(/^#/, "");
  const [rawPath = "/assistant", search = ""] = rawHash.split("?");
  const params = new URLSearchParams(search);

  const { page, shareToken, skillAction } = normalizePath(rawPath);

  return {
    page,
    view: params.get("view") || DEFAULT_ROUTE.view,
    preset: params.get("preset") || DEFAULT_ROUTE.preset,
    sessionId: params.get("sessionId") || DEFAULT_ROUTE.sessionId,
    type: params.get("type") || DEFAULT_ROUTE.type,
    skill: params.get("skill") || DEFAULT_ROUTE.skill,
    skillAction: skillAction || DEFAULT_ROUTE.skillAction,
    skillMode: params.get("mode") || DEFAULT_ROUTE.skillMode,
    nodeId: params.get("nodeId") || DEFAULT_ROUTE.nodeId,
    reviewId: params.get("reviewId") || DEFAULT_ROUTE.reviewId,
    reviewAction: params.get("reviewAction") || DEFAULT_ROUTE.reviewAction,
    q: params.get("q") || DEFAULT_ROUTE.q,
    shareToken: shareToken || params.get("shareToken") || DEFAULT_ROUTE.shareToken
  };
}

export function buildHash(page, params = {}) {
  if (page === "shared" && params.shareToken) {
    return `#/shared/${encodeURIComponent(params.shareToken)}`;
  }
  if (page === "skill" && ["new", "import"].includes(params.action)) {
    const { action, ...searchParams } = params;
    const search = new URLSearchParams(
      Object.entries(searchParams).filter(([, value]) => value !== undefined && value !== null && value !== "")
    );
    const query = search.toString();
    return `#/skill/${action}${query ? `?${query}` : ""}`;
  }
  const search = new URLSearchParams(
    Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== "")
  );
  const query = search.toString();
  return `#/${page}${query ? `?${query}` : ""}`;
}

export function buildAppHref(page, params = {}) {
  return `/${buildHash(page, params)}`;
}

function normalizePath(rawPath) {
  const parts = rawPath.replace(/^\/+/, "").split("/").filter(Boolean);
  const page = parts[0] || DEFAULT_ROUTE.page;
  if (page === "shared") {
    return {
      page,
      shareToken: parts[1] ? decodeURIComponent(parts[1]) : "",
      skillAction: ""
    };
  }
  if (["assistant", "tasks", "knowledge", "skill"].includes(page)) {
    return {
      page,
      shareToken: "",
      skillAction: page === "skill" && ["new", "import"].includes(parts[1]) ? parts[1] : ""
    };
  }
  return { page: DEFAULT_ROUTE.page, shareToken: "", skillAction: "" };
}
