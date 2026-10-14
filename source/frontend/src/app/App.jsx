import { useEffect, useState } from "react";
import AppShell from "../components/layout/AppShell";
import AgentAccessGatePage from "../pages/AgentAccessGatePage";
import AiAssistantPage from "../pages/AiAssistantPage";
import KnowledgePage from "../pages/KnowledgePage";
import LoginPage from "../pages/LoginPage";
import MyTasksPage from "../pages/MyTasksPage";
import SharedSessionsPage from "../pages/SharedSessionsPage";
import SkillPage, { createSkillRunState } from "../pages/SkillPage";
import { getKnowledgeScopeByPath, knowledgeScopes, visibleKnowledgeTypeIds } from "../data/knowledgeScopes";
import { deleteAssistantSession, listAssistantSessions, listSharedAssistantSessions } from "../services/assistantApi";
import { getCurrentUser, logout } from "../services/authApi";
import { authExpiredEventName } from "../services/authEvents";
import { getKnowledgeChildren, getKnowledgeIndex } from "../services/workspaceApi";
import { buildAppHref, buildHash, parseHashRoute } from "../utils/hashRoute";

const recentSessionLimit = 50;

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

function resolveCitationKnowledgeType(path) {
  return getKnowledgeScopeByPath(path)?.id || null;
}

function resolveNodeIdFromCitationPath(path) {
  if (!path) {
    return null;
  }
  const normalized = path.replace(/\\/g, "/");
  for (const { rootPath } of knowledgeScopes) {
    const idx = normalized.indexOf(rootPath);
    if (idx !== -1) {
      return normalized.slice(idx);
    }
  }
  return null;
}

export default function App() {
  const [route, setRoute] = useState(parseHashRoute);
  const [sidebarExpanded, setSidebarExpanded] = useState(true);
  const [assistantSessions, setAssistantSessions] = useState([]);
  const [sharedAssistantSessions, setSharedAssistantSessions] = useState([]);
  const [activeAssistantSessionIds, setActiveAssistantSessionIds] = useState([]);
  const [lastAssistantSessionId, setLastAssistantSessionId] = useState(() => parseHashRoute().sessionId || "");
  const [lastKnowledgeNodeIds, setLastKnowledgeNodeIds] = useState(() => {
    const init = parseHashRoute();
    return init.page === "knowledge" && init.nodeId ? { [init.type]: init.nodeId } : {};
  });
  const [lastKnowledgeType, setLastKnowledgeType] = useState(() => {
    const init = parseHashRoute();
    return init.page === "knowledge" ? init.type : "";
  });
  const [pendingAssistantReference, setPendingAssistantReference] = useState(null);
  const [assistantDraftReferences, setAssistantDraftReferences] = useState([]);
  const [skillCreateRunState, setSkillCreateRunState] = useState(createSkillRunState);
  const [unreadSkillFolderIds, setUnreadSkillFolderIds] = useState(() => new Set());
  const [requirementReviewBadge, setRequirementReviewBadge] = useState(null);
  const [currentUser, setCurrentUser] = useState(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [authError, setAuthError] = useState("");
  const [loginRequested, setLoginRequested] = useState(false);

  useEffect(() => {
    function handleHashChange() {
      setRoute(parseHashRoute());
    }

    window.addEventListener("hashchange", handleHashChange);
    window.addEventListener("popstate", handleHashChange);
    return () => {
      window.removeEventListener("hashchange", handleHashChange);
      window.removeEventListener("popstate", handleHashChange);
    };
  }, []);

  useEffect(() => {
    if (route.page === "assistant") {
      setLastAssistantSessionId(route.sessionId || "");
    }
    if (route.page === "knowledge") {
      setLastKnowledgeType(route.type);
      if (route.nodeId) {
        setLastKnowledgeNodeIds((current) => ({ ...current, [route.type]: route.nodeId }));
      }
    }
  }, [route.page, route.sessionId, route.type, route.nodeId]);

  useEffect(() => {
    function handleGlobalNewSessionShortcut(event) {
      if (!currentUser) {
        return;
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        navigate("assistant");
      }
    }

    window.addEventListener("keydown", handleGlobalNewSessionShortcut);
    return () => window.removeEventListener("keydown", handleGlobalNewSessionShortcut);
  }, [currentUser]);

  useEffect(() => {
    let disposed = false;

    async function hydrateCurrentUser() {
      setAuthLoading(true);
      setAuthError("");

      try {
        const user = await getCurrentUser();
        if (!disposed) {
          setCurrentUser(user);
        }
      } catch (error) {
        if (!disposed) {
          setCurrentUser(null);
          setAuthError(error.message);
        }
      } finally {
        if (!disposed) {
          setAuthLoading(false);
        }
      }
    }

    hydrateCurrentUser();
    return () => {
      disposed = true;
    };
  }, []);

  useEffect(() => {
    function handleAuthExpired(event) {
      setCurrentUser(null);
      setAssistantSessions([]);
      setSharedAssistantSessions([]);
      setActiveAssistantSessionIds([]);
      setPendingAssistantReference(null);
      setAssistantDraftReferences([]);
      setSkillCreateRunState(createSkillRunState());
      setUnreadSkillFolderIds(new Set());
      setRequirementReviewBadge(null);
      setAuthError(event.detail?.message || "登录状态已过期，请重新登录。");
    }

    window.addEventListener(authExpiredEventName, handleAuthExpired);
    return () => window.removeEventListener(authExpiredEventName, handleAuthExpired);
  }, []);

  useEffect(() => {
    if (currentUser && !window.location.hash) {
      navigate("assistant");
    }
  }, [currentUser]);

  useEffect(() => {
    if (!currentUser) {
      return undefined;
    }

    return scheduleIdleTask(() => {
      void getKnowledgeIndex().catch(() => {});
    });
  }, [currentUser]);

  useEffect(() => {
    let disposed = false;

    async function loadRequirementReviewBadge() {
      if (!currentUser) {
        setRequirementReviewBadge(null);
        return;
      }
      try {
        const payload = await getKnowledgeChildren("requirements");
        if (!disposed) {
          setRequirementReviewBadge(payload.root?.review_badge || null);
        }
      } catch {
        if (!disposed) {
          setRequirementReviewBadge(null);
        }
      }
    }

    loadRequirementReviewBadge();
    return () => {
      disposed = true;
    };
  }, [currentUser]);

  useEffect(() => {
    let disposed = false;

    async function loadSidebarSessions() {
      if (!currentUser) {
        return;
      }
      try {
        const [sessions, sharedSessions] = await Promise.all([
          listAssistantSessions({ limit: recentSessionLimit }),
          listSharedAssistantSessions()
        ]);
        if (!disposed) {
          setAssistantSessions(sessions);
          setSharedAssistantSessions(sharedSessions);
        }
      } catch {
        if (!disposed) {
          setAssistantSessions([]);
          setSharedAssistantSessions([]);
        }
      }
    }

    loadSidebarSessions();
    return () => {
      disposed = true;
    };
  }, [currentUser]);

  function navigate(page, params = {}) {
    if (page === "knowledge") {
      if (!visibleKnowledgeTypeIds.has(params.type)) {
        params = { ...params, type: "requirements", nodeId: undefined };
      }
      if (
        route.page !== "knowledge" &&
        lastKnowledgeType &&
        visibleKnowledgeTypeIds.has(lastKnowledgeType) &&
        params.type === "requirements" &&
        lastKnowledgeType !== "requirements"
      ) {
        params = { ...params, type: lastKnowledgeType };
      }
      if (!params.nodeId) {
        const savedNodeId = lastKnowledgeNodeIds[params.type];
        if (savedNodeId) {
          params = { ...params, nodeId: savedNodeId };
        }
      }
    }
    const nextHash = buildHash(page, params);
    const nextHref = buildAppHref(page, params);
    if (window.location.pathname === "/" && window.location.hash === nextHash) {
      setRoute(parseHashRoute());
      return;
    }
    window.history.pushState(null, "", nextHref);
    setRoute(parseHashRoute());
  }

  function handleReferenceToAssistant(reference) {
    const token = `${reference.key}-${Date.now()}`;
    setAssistantDraftReferences((current) => {
      if (current.some((item) => item.key === reference.key)) {
        return current;
      }
      return current.concat(reference);
    });
    setPendingAssistantReference({ ...reference, token });
    navigate("assistant", { sessionId: lastAssistantSessionId || undefined });
  }

  function handleTaskNavigateAssistant(params = {}) {
    const { reference, ...routeParams } = params;
    if (reference) {
      const token = `${reference.key}-${Date.now()}`;
      setAssistantDraftReferences([reference]);
      setPendingAssistantReference({ ...reference, token });
    }
    navigate("assistant", routeParams);
  }

  function handleNavigateToCitation(citation) {
    const type = resolveCitationKnowledgeType(citation.path);
    const nodeId = resolveNodeIdFromCitationPath(citation.path);
    if (type && nodeId) {
      navigate("knowledge", { type, nodeId });
    }
  }

  function handleConsumeAssistantReference(token) {
    setPendingAssistantReference((current) => (current?.token === token ? null : current));
  }

  async function handleLogout() {
    try {
      await logout();
    } catch {
      // 忽略退出时的网络异常，前端仍然清理本地状态
    }

    setAssistantSessions([]);
    setSharedAssistantSessions([]);
    setCurrentUser(null);
    setPendingAssistantReference(null);
    setAssistantDraftReferences([]);
    setSkillCreateRunState(createSkillRunState());
    setUnreadSkillFolderIds(new Set());
    setRequirementReviewBadge(null);
  }

  async function handleDeleteAssistantSession(sessionId) {
    await deleteAssistantSession(sessionId);
    const [sessions, sharedSessions] = await Promise.all([
      listAssistantSessions({ limit: recentSessionLimit }),
      listSharedAssistantSessions()
    ]);
    setAssistantSessions(sessions);
    setSharedAssistantSessions(sharedSessions);
    setLastAssistantSessionId((current) => (current === sessionId ? "" : current));

    if (route.page === "assistant" && route.sessionId === sessionId) {
      navigate("assistant");
    }
  }

  if (!currentUser) {
    if (route.page === "shared" && !loginRequested) {
      return (
        <SharedSessionsPage
          shareToken={route.shareToken || ""}
          currentUser={null}
          onRequireLogin={() => setLoginRequested(true)}
          onNavigateShared={(shareToken) =>
            shareToken ? navigate("shared", { shareToken }) : navigate("assistant", { view: "history" })
          }
          onNavigateCitation={handleNavigateToCitation}
        />
      );
    }
    return (
      <LoginPage
        onAuthenticated={(user) => {
          setLoginRequested(false);
          setCurrentUser(user);
        }}
        authError={authError}
      />
    );
  }

  if (currentUser.status === "blocked") {
    return (
      <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: "16px", fontFamily: "var(--font-sans)" }}>
        <h2 style={{ margin: 0 }}>账号已被封禁</h2>
        <p style={{ margin: 0, color: "var(--text-soft)" }}>如有疑问请联系管理员。</p>
        <button className="button button-secondary" onClick={handleLogout}>退出登录</button>
      </div>
    );
  }

  if (currentUser.agent_access !== "active") {
    return (
      <AgentAccessGatePage
        user={currentUser}
        onUserUpdated={setCurrentUser}
        onLogout={handleLogout}
      />
    );
  }

  const shellActivePage = route.page === "shared" ? "assistant" : route.page;

  return (
    <AppShell
      activePage={shellActivePage}
      route={route}
      assistantSessions={assistantSessions}
      sharedAssistantSessions={sharedAssistantSessions}
      activeAssistantSessionIds={activeAssistantSessionIds}
      currentUser={currentUser}
      requirementReviewBadge={requirementReviewBadge}
      sidebarExpanded={sidebarExpanded}
      onToggleSidebar={() => setSidebarExpanded((current) => !current)}
      onNavigate={navigate}
      onDeleteAssistantSession={handleDeleteAssistantSession}
      onLogout={handleLogout}
    >
      {route.page === "knowledge" ? (
        <KnowledgePage
          key={route.type}
          knowledgeType={route.type}
          initialNodeId={route.nodeId || undefined}
          initialReviewId={route.reviewId || ""}
          initialReviewAction={route.reviewAction || ""}
          onReferenceNode={handleReferenceToAssistant}
          onNavigateKnowledgeNode={(nodeId, params = {}) => navigate("knowledge", { type: route.type, nodeId, ...params })}
          onRequirementReviewBadgeChange={setRequirementReviewBadge}
        />
      ) : null}

      {route.page === "tasks" ? (
        <MyTasksPage
          onReferenceNode={handleReferenceToAssistant}
          onNavigateAssistant={handleTaskNavigateAssistant}
          onNavigateKnowledgeNode={(nodeId, params = {}) => navigate("knowledge", { type: "requirements", nodeId, ...params })}
        />
      ) : null}

      {route.page === "skill" ? (
        <SkillPage
          selectedSkillId={route.skill}
          createAction={route.skillAction}
          createMode={route.skillMode}
          currentUser={currentUser}
          onReferenceNode={handleReferenceToAssistant}
          onNavigateAssistant={(params = {}) => navigate("assistant", params)}
          onNavigateSkill={(params = {}) => navigate("skill", params)}
          createRunState={skillCreateRunState}
          onCreateRunStateChange={setSkillCreateRunState}
          unreadSkillFolderIds={unreadSkillFolderIds}
          onUnreadSkillFolderIdsChange={setUnreadSkillFolderIds}
        />
      ) : null}

      {route.page === "assistant" ? (
        <AiAssistantPage
          sessionId={route.sessionId || null}
          view={route.view || "chat"}
          historyQuery={route.q || ""}
          currentUser={currentUser}
          onNavigateAssistant={(params = {}) => navigate("assistant", params)}
          onDeleteAssistantSession={handleDeleteAssistantSession}
          onSessionsChange={setAssistantSessions}
          onSharedSessionsChange={setSharedAssistantSessions}
          onActiveSessionsChange={setActiveAssistantSessionIds}
          referencedSourcesDraft={assistantDraftReferences}
          onReferencedSourcesDraftChange={setAssistantDraftReferences}
          pendingReference={pendingAssistantReference}
          onConsumePendingReference={handleConsumeAssistantReference}
          onNavigateCitation={handleNavigateToCitation}
        />
      ) : null}

      {route.page === "shared" ? (
        <SharedSessionsPage
          shareToken={route.shareToken || ""}
          currentUser={currentUser}
          onNavigateShared={(shareToken) =>
            shareToken ? navigate("shared", { shareToken }) : navigate("assistant", { view: "history" })
          }
          onNavigateCitation={handleNavigateToCitation}
        />
      ) : null}
    </AppShell>
  );
}
