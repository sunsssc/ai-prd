const REVIEW_STATUS_LABELS = {
  approve: "独立审核通过",
  reject: "独立审核未通过",
  manual: "独立审核要求人工处理",
};

const GENERATOR_REVIEW_LABELS = {
  agree: "生成 Agent 已采纳审核意见",
  disagree: "生成 Agent 未采纳审核意见",
};

export function getBusinessDocItemDisplay(item) {
  if (item.status === "failed") {
    return {
      tone: "error",
      title: "检查失败",
      description: "本次分析没有完成，请根据错误原因修复后重试。",
    };
  }
  if (item.status === "running") {
    return {
      tone: "running",
      title: "正在检查",
      description: "Agent 正在核对最新代码证据与业务文档。",
    };
  }
  if (item.status === "pending") {
    return {
      tone: "pending",
      title: "等待检查",
      description: "任务已进入队列，尚未开始分析。",
    };
  }
  if (item.status === "completed" && item.result_type === "no_update_needed") {
    return {
      tone: "success",
      title: "检查完成 · 无需更新",
      description: "代码变化未涉及本文档需要维护的稳定业务规则，因此没有生成更新提案。",
    };
  }
  if (item.status === "completed" && item.apply_status === "applied") {
    return {
      tone: "success",
      title: "检查完成 · 已应用更新",
      description: "更新提案已经应用到业务文档。",
    };
  }
  if (item.status === "completed" && item.apply_status === "ignored") {
    return {
      tone: "pending",
      title: "检查完成 · 已忽略",
      description: "人工确认后未应用该更新提案，业务文档保持不变。",
    };
  }
  if (item.status === "completed" && item.result_type === "new_doc_candidate") {
    return {
      tone: "attention",
      title: "检查完成 · 发现新文档候选",
      description: "发现可能需要新增的业务文档，请人工确认。",
    };
  }
  if (item.status === "completed" && item.apply_status === "manual_required") {
    return {
      tone: "attention",
      title: "检查完成 · 等待人工处理",
      description: "已生成更新提案，但不满足自动应用条件，请人工确认。",
    };
  }
  return {
    tone: "pending",
    title: item.status === "completed" ? "检查完成" : "状态待确认",
    description: item.status === "completed" ? "本次分析流程已结束。" : "请刷新后查看最新状态。",
  };
}

export function getBusinessDocReviewLabels(item) {
  if (item.review_status === "not_required") return [];
  return [
    REVIEW_STATUS_LABELS[item.review_status],
    GENERATOR_REVIEW_LABELS[item.generator_review],
  ].filter(Boolean);
}

export function formatBusinessDocConfidence(confidence) {
  return typeof confidence === "number" ? `判断置信度 ${Math.round(confidence * 100)}%` : "";
}

export function getHighConfidenceManualUpdates(items, threshold = 0.9) {
  return (items || []).filter(
    (item) =>
      item.update_id &&
      item.apply_status === "manual_required" &&
      typeof item.confidence === "number" &&
      item.confidence > threshold,
  );
}
