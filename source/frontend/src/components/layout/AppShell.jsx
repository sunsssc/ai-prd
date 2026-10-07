import Sidebar from "./Sidebar";

export default function AppShell({
  activePage,
  route,
  assistantSessions,
  sharedAssistantSessions,
  activeAssistantSessionIds,
  currentUser,
  requirementReviewBadge,
  sidebarExpanded,
  onToggleSidebar,
  onNavigate,
  onDeleteAssistantSession,
  onLogout,
  children
}) {
  const mainClassName = [
    "layout-main",
    activePage === "assistant" || activePage === "shared" ? "layout-main-assistant" : "layout-main-workspace"
  ].join(" ");

  return (
    <div className={`app-shell ${sidebarExpanded ? "app-shell-expanded" : ""}`.trim()}>
      <Sidebar
        activePage={activePage}
        route={route}
        assistantSessions={assistantSessions}
        sharedAssistantSessions={sharedAssistantSessions}
        activeAssistantSessionIds={activeAssistantSessionIds}
        currentUser={currentUser}
        requirementReviewBadge={requirementReviewBadge}
        expanded={sidebarExpanded}
        onToggleSidebar={onToggleSidebar}
        onNavigate={onNavigate}
        onDeleteAssistantSession={onDeleteAssistantSession}
        onLogout={onLogout}
      />
      <main className={mainClassName}>{children}</main>
    </div>
  );
}
