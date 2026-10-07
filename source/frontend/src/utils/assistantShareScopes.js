// 会话分享范围：指定成员 / 仅 Agent 用户 / 所有注册用户 / 任何人（含未登录）。
// 后端模型：share_type=members 表示指定成员；share_type=public 时由 visibility 决定可见范围。
export const SHARE_SCOPES = [
  {
    key: "members",
    label: "指定成员",
    hint: "仅选中的成员可见",
    description: "",
    shareType: "members",
    visibility: "anyone"
  },
  {
    key: "agent",
    label: "仅 Agent 用户",
    hint: "需 Agent 使用权限",
    description: "只有获得 Agent 使用权限的用户能通过该链接查看完整对话、引用与附件，你可随时撤销。",
    shareType: "public",
    visibility: "agent"
  },
  {
    key: "registered",
    label: "所有注册用户",
    hint: "登录后可见",
    description: "所有登录后的注册用户都能通过该链接查看完整对话、引用与附件，你可随时撤销。",
    shareType: "public",
    visibility: "registered"
  },
  {
    key: "anyone",
    label: "任何人",
    hint: "无需登录",
    description: "任何获得链接的人都能在未登录状态下查看完整对话、引用与附件，你可随时撤销。",
    shareType: "public",
    visibility: "anyone"
  }
];

export function getShareScope(key) {
  return SHARE_SCOPES.find((scope) => scope.key === key) || SHARE_SCOPES[SHARE_SCOPES.length - 1];
}

export function getShareScopeLabel(key) {
  return getShareScope(key).label;
}

// 从后端返回的 share 对象推导当前范围 key。
export function shareScopeFromShare(share) {
  if (!share) {
    return null;
  }
  if (share.share_type === "members") {
    return "members";
  }
  return share.visibility || "anyone";
}

// 生成保存分享时发送给后端的 payload。
export function buildSharePayload(scopeKey, memberUserIds = []) {
  const scope = getShareScope(scopeKey);
  if (scope.key === "members") {
    return {
      share_type: "members",
      visibility: "anyone",
      member_user_ids: memberUserIds
    };
  }
  return {
    share_type: "public",
    visibility: scope.visibility,
    member_user_ids: []
  };
}
