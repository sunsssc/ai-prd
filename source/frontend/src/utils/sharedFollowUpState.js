function authorName(user) {
  return user?.name || user?.email || "未知用户";
}

export function isSharedFollowUpInProgress(requestedBy, session) {
  return Boolean(requestedBy?.user_id && session?.has_active_turn);
}

export function createSharedLiveTurnState(turnId = "") {
  return {
    turnId,
    assistantText: "",
    activity: "正在连接实时回复…",
    activities: [],
    skills: [],
    citations: []
  };
}

export function reduceSharedLiveTurnState(current, event) {
  if (event?.type === "delta") {
    return { ...current, assistantText: `${current.assistantText}${event.delta || ""}` };
  }
  if (event?.type === "activity") {
    const activity = event.message || current.activity;
    return {
      ...current,
      activity,
      activities: current.activities[current.activities.length - 1] === activity
        ? current.activities
        : current.activities.concat(activity)
    };
  }
  if (event?.type === "tool_use") {
    const activity = `正在调用工具：${event.tool_name || "工具"}`;
    return {
      ...current,
      activity,
      activities: current.activities[current.activities.length - 1] === activity
        ? current.activities
        : current.activities.concat(activity)
    };
  }
  if (event?.type === "skill_use") {
    const activity = `正在使用 Skill：${event.skill_name || event.skill_id || "Skill"}`;
    const skillKey = event.skill_id || event.skill_name || "Skill";
    return {
      ...current,
      activity,
      activities: current.activities[current.activities.length - 1] === activity
        ? current.activities
        : current.activities.concat(activity),
      skills: current.skills.some((item) => (item.skill_id || item.skill_name || "Skill") === skillKey)
        ? current.skills
        : current.skills.concat(event)
    };
  }
  if (event?.type === "citations") {
    return { ...current, citations: event.citations || [] };
  }
  return current;
}

export function getSharedTurnPresentation(requestedBy, currentUser) {
  const isOwnFollowUp = Boolean(
    requestedBy?.user_id && currentUser?.user_id && requestedBy.user_id === currentUser.user_id
  );

  if (isOwnFollowUp) {
    return {
      title: "正在处理你的追问",
      description: "本轮完成后，你会在这里看到最新回复。",
      isOwnFollowUp: true
    };
  }

  return {
    title: requestedBy ? `${authorName(requestedBy)} 正在追问` : "AI 正在生成回复",
    description: requestedBy
      ? "本轮完成前，其他查看者暂时不能继续追问。"
      : "本轮完成后，所有查看者都会获取最新回复。",
    isOwnFollowUp: false
  };
}
