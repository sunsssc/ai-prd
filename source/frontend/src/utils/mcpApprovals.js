export function upsertMcpApproval(approvals, incoming) {
  const index = approvals.findIndex((item) => item.approval_id === incoming.approval_id);
  if (index < 0) return approvals.concat(incoming);
  if (approvals[index].status !== "pending" && incoming.status === "pending") return approvals;
  return approvals.map((item, itemIndex) => itemIndex === index ? incoming : item);
}

export function canDecideMcpApproval(approval, now = Date.now()) {
  return approval.status === "pending" && Date.parse(approval.expires_at) > now;
}
