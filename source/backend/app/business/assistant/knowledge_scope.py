from __future__ import annotations

from dataclasses import dataclass
import re

from app.business.assistant.models import AssistantContextInput


@dataclass(frozen=True, slots=True)
class KnowledgeScope:
    scope_id: str
    label: str
    description: str
    relative_path: str
    intent_keywords: tuple[str, ...]

    def absolute_path(self, working_directory: str) -> str:
        return self.runtime_path(working_directory)

    def runtime_path(self, working_directory: str) -> str:
        if working_directory.startswith("/"):
            return f"{working_directory.rstrip('/')}/knowledge/{self.relative_path}"
        return f"{working_directory.rstrip('/')}/knowledge/{self.relative_path}"


KNOWLEDGE_SCOPES: dict[str, KnowledgeScope] = {
    "requirements": KnowledgeScope(
        scope_id="requirements",
        label="@需求文档",
        description="需求方案、PRD、ClickUp 任务、原型、验收和页面流程",
        relative_path="requirements",
        intent_keywords=(
            "需求",
            "prd",
            "产品方案",
            "原型",
            "页面",
            "交互",
            "流程",
            "验收",
            "目标",
            "范围",
            "方案",
            "功能",
            "评审",
        ),
    ),
    "business": KnowledgeScope(
        scope_id="business",
        label="@业务文档",
        description="业务规则、口径、状态机、结算和运营约束",
        relative_path="business-docs",
        intent_keywords=(
            "业务",
            "规则",
            "口径",
            "结算",
            "返佣",
            "返现",
            "优惠券",
            "状态",
            "状态机",
            "资格",
            "风控",
            "补偿",
            "运营",
            "域",
        ),
    ),
    "code": KnowledgeScope(
        scope_id="code",
        label="@代码范围",
        description="代码实现、接口、模块、任务、日志和回归影响",
        relative_path="code",
        intent_keywords=(
            "代码",
            "实现",
            "接口",
            "模块",
            "函数",
            "类",
            "仓库",
            "回归",
            "报错",
            "异常",
            "bug",
            "sql",
            "日志",
            "任务",
            "服务",
            "链路",
            "影响分析",
        ),
    ),
}

CODE_PATTERN = re.compile(r"[A-Za-z0-9_./-]+\.(py|ts|tsx|js|jsx|java|go|sql|json|yaml|yml|md)\b")


def extract_explicit_knowledge_scopes(context_items: list[AssistantContextInput]) -> list[KnowledgeScope]:
    scopes: list[KnowledgeScope] = []
    seen: set[str] = set()

    for item in context_items:
        if item.source_type != "knowledge_scope":
            continue
        raw_scope = str(item.metadata.get("scope") or item.context_key).strip()
        scope_id = _normalize_scope_id(raw_scope)
        if not scope_id or scope_id in seen:
            continue
        scope = KNOWLEDGE_SCOPES.get(scope_id)
        if scope is None:
            continue
        seen.add(scope_id)
        scopes.append(scope)

    return scopes


def infer_knowledge_scopes_from_message(message: str) -> list[KnowledgeScope]:
    normalized = message.lower()
    scores = {scope_id: 0 for scope_id in KNOWLEDGE_SCOPES}

    for scope_id, scope in KNOWLEDGE_SCOPES.items():
        for keyword in scope.intent_keywords:
            if keyword.lower() in normalized:
                scores[scope_id] += 2

    if CODE_PATTERN.search(message):
        scores["code"] += 3

    if any(token in message for token in ("接口", "实现", "回归", "报错", "异常", "堆栈")):
        scores["code"] += 2

    if any(token in message for token in ("规则", "口径", "状态", "结算", "返佣", "补偿")):
        scores["business"] += 2

    if any(token in message for token in ("需求", "PRD", "prd", "原型", "页面", "交互", "验收")):
        scores["requirements"] += 2

    ranked_scope_ids = [scope_id for scope_id, score in sorted(scores.items(), key=lambda item: item[1], reverse=True) if score > 0]
    if not ranked_scope_ids:
        ranked_scope_ids = ["requirements"]

    return [KNOWLEDGE_SCOPES[scope_id] for scope_id in ranked_scope_ids]


def build_knowledge_scope_instruction(
    *,
    working_directory: str,
    explicit_scopes: list[KnowledgeScope],
    inferred_scopes: list[KnowledgeScope],
) -> str:
    priority_scopes = explicit_scopes or inferred_scopes
    priority_scope_ids = {scope.scope_id for scope in priority_scopes}
    if explicit_scopes:
        heading = "本轮显式知识范围与知识库目录："
        search_rule = "本轮已提供显式知识范围，先搜索标记为“本轮优先”的目录；必要时再补充其他目录。"
    elif inferred_scopes:
        heading = "本轮自动推断的优先知识范围与知识库目录："
        search_rule = "本轮未提供显式知识范围，按问题意图从标记为“本轮优先”的目录开始搜索。"
    else:
        heading = "知识库目录："
        search_rule = "根据问题意图选择最相关的目录开始搜索。"

    all_scope_lines = [
        f"- {scope.label}{'（本轮优先）' if scope.scope_id in priority_scope_ids else ''}："
        f"{scope.description}，目录 `{scope.runtime_path(working_directory)}`"
        for scope in KNOWLEDGE_SCOPES.values()
    ]
    return "\n\n".join(
        [
            heading + "\n" + "\n".join(all_scope_lines),
            (
                "检索规则：\n"
                f"- {search_rule}\n"
                "- 搜索时先定位目录和文件，再读取命中文件；不要跳过搜索直接下结论。"
            ),
        ]
    )


def build_scope_context_inputs(
    scopes: list[KnowledgeScope],
    *,
    working_directory: str,
    explicit: bool,
) -> list[AssistantContextInput]:
    return [
        AssistantContextInput(
            context_key=f"knowledge-scope:{scope.scope_id}",
            label=scope.label,
            source_type="knowledge_scope",
            source_uri=scope.absolute_path(working_directory),
            metadata={
                "scope": scope.scope_id,
                "explicit": explicit,
            },
        )
        for scope in scopes
    ]


def resolve_knowledge_scope_id_from_path(path: str | None) -> str | None:
    if not path:
        return None

    normalized = path.replace("\\", "/").lower()
    markers = {
        "requirements": "knowledge/requirements",
        "business": "knowledge/business-docs",
        "code": "knowledge/code",
    }
    for scope_id, marker in markers.items():
        if normalized == marker or normalized.endswith(f"/{marker}") or f"/{marker}/" in normalized:
            return scope_id
    return None


def _normalize_scope_id(raw_scope: str) -> str | None:
    normalized = raw_scope.strip().lower()
    if normalized.startswith("knowledge-scope:"):
        normalized = normalized.split(":", 1)[1]
    if normalized in KNOWLEDGE_SCOPES:
        return normalized
    return None
