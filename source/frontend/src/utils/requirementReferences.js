import { buildAppHref } from "./hashRoute.js";
import { listKnowledgeFiles } from "./knowledgeIndex.js";

const REQUIREMENT_TASK_PATH_PREFIX = "tasks/";
const REQUIREMENT_FILE_PATTERN = /^(.*)__(86[a-z0-9]+)\.md$/i;
const REQUIREMENT_ID_PATTERN = /#?(86[a-z0-9]{5,})/gi;
const REQUIREMENT_LINK_LABEL_PATTERN = /^(?:需求\s*)?#?(86[a-z0-9]{5,})$/i;
const SKIPPED_MARKDOWN_NODE_TYPES = new Set(["code", "definition", "html", "linkReference"]);

function requirementPathPriority(path = "") {
  return String(path).replace(/\\/g, "/").includes("/tasks/_deleted/") ? 0 : 1;
}

export function buildRequirementReferenceMap(indexPayload) {
  const references = new Map();

  for (const file of listKnowledgeFiles(indexPayload, "requirements")) {
    if (!file.relativePath.startsWith(REQUIREMENT_TASK_PATH_PREFIX)) {
      continue;
    }

    const match = file.name.match(REQUIREMENT_FILE_PATTERN);
    if (!match) {
      continue;
    }

    const [, title, rawId] = match;
    const id = rawId.toLowerCase();
    const candidate = {
      id,
      title,
      path: file.fullPath,
      href: buildAppHref("knowledge", { type: "requirements", nodeId: file.fullPath })
    };
    const current = references.get(id);
    if (!current || requirementPathPriority(candidate.path) > requirementPathPriority(current.path)) {
      references.set(id, candidate);
    }
  }

  return references;
}

function linkNode(reference) {
  return {
    type: "link",
    url: reference.href,
    title: `打开需求文档：${reference.title}`,
    children: [{ type: "text", value: reference.title }]
  };
}

function titleCandidates(title) {
  const withPrefixSpace = title.replace(/^(【[^】]+】)(?!\s)/, "$1 ");
  return withPrefixSpace === title ? [title] : [title, withPrefixSpace];
}

function titleMatches(value, references) {
  const matches = [];
  for (const reference of references.values()) {
    for (const title of titleCandidates(reference.title)) {
      let index = value.indexOf(title);
      while (index >= 0) {
        matches.push({ start: index, end: index + title.length, reference });
        index = value.indexOf(title, index + title.length);
      }
    }
  }
  return matches;
}

function idMatches(value, references) {
  return [...value.matchAll(REQUIREMENT_ID_PATTERN)]
    .map((match) => ({
      start: match.index,
      end: match.index + match[0].length,
      reference: references.get(match[1].toLowerCase())
    }))
    .filter((match) => match.reference);
}

function linkedTextNodes(value, references) {
  const matches = [];
  for (const match of [...titleMatches(value, references), ...idMatches(value, references)].sort(
    (first, second) => first.start - second.start || second.end - first.end
  )) {
    if (!matches.length || match.start >= matches[matches.length - 1].end) {
      matches.push(match);
    }
  }
  if (!matches.length) {
    return null;
  }

  const nodes = [];
  let cursor = 0;
  for (const match of matches) {
    if (match.start > cursor) {
      nodes.push({ type: "text", value: value.slice(cursor, match.start) });
    }
    nodes.push(linkNode(match.reference));
    cursor = match.end;
  }
  if (cursor < value.length) {
    nodes.push({ type: "text", value: value.slice(cursor) });
  }
  return nodes;
}

function collectCitedReferences(tree, references) {
  const cited = new Map();

  function collect(node) {
    const values = [];
    if (typeof node?.value === "string") {
      values.push(node.value);
    }
    if (typeof node?.url === "string") {
      values.push(node.url);
    }
    for (const value of values) {
      for (const match of value.matchAll(REQUIREMENT_ID_PATTERN)) {
        const reference = references.get(match[1].toLowerCase());
        if (reference) {
          cited.set(reference.id, reference);
        }
      }
    }
    for (const child of node?.children || []) {
      collect(child);
    }
  }

  collect(tree);
  return cited;
}

function collectLinkedTitleIds(tree, references) {
  const result = new Set();

  function collect(node) {
    if (node?.type === "text") {
      for (const match of titleMatches(node.value || "", references)) {
        result.add(match.reference.id);
      }
    }
    for (const child of node?.children || []) {
      collect(child);
    }
  }

  collect(tree);
  return result;
}

function inlineCodeReference(value, references) {
  const exactMatch = String(value || "").trim().match(REQUIREMENT_LINK_LABEL_PATTERN);
  if (exactMatch) {
    return references.get(exactMatch[1].toLowerCase()) || null;
  }
  const pathMatch = String(value || "").match(/__(86[a-z0-9]+)\.md(?:$|[?#])/i);
  return pathMatch ? references.get(pathMatch[1].toLowerCase()) || null : null;
}

export function linkRequirementReferencesInTree(tree, references) {
  if (!tree || !references?.size) {
    return tree;
  }
  const citedReferences = collectCitedReferences(tree, references);
  if (!citedReferences.size) {
    return tree;
  }
  const linkedTitleIds = collectLinkedTitleIds(tree, citedReferences);

  function transform(node) {
    if (!node?.children) {
      return;
    }
    if (node.type === "link") {
      const label = node.children.length === 1 && node.children[0]?.type === "text" ? node.children[0].value.trim() : "";
      const match = label.match(REQUIREMENT_LINK_LABEL_PATTERN);
      const titleReference = [...citedReferences.values()].find(
        (reference) => titleCandidates(reference.title).includes(label)
      );
      const reference = match ? citedReferences.get(match[1].toLowerCase()) : titleReference;
      if (reference) {
        node.url = reference.href;
        node.title = `打开需求文档：${reference.title}`;
        node.children = [{ type: "text", value: reference.title }];
      }
      return;
    }
    if (SKIPPED_MARKDOWN_NODE_TYPES.has(node.type)) {
      return;
    }

    const nextChildren = [];
    for (const child of node.children) {
      if (child?.type === "text") {
        nextChildren.push(...(linkedTextNodes(child.value || "", citedReferences) || [child]));
        continue;
      }
      if (child?.type === "inlineCode") {
        const reference = inlineCodeReference(child.value, citedReferences);
        nextChildren.push(reference && !linkedTitleIds.has(reference.id) ? linkNode(reference) : child);
        continue;
      }
      transform(child);
      nextChildren.push(child);
    }
    node.children = nextChildren;
  }

  transform(tree);
  return tree;
}

export function remarkRequirementReferences({ references } = {}) {
  return (tree) => linkRequirementReferencesInTree(tree, references);
}

export function resolveRequirementReferenceHref(path = "", references = null) {
  if (!path || !references?.size) {
    return "";
  }

  let normalized;
  try {
    normalized = decodeURIComponent(String(path)).replace(/\\/g, "/");
  } catch {
    return "";
  }
  const match = normalized.match(/__(86[a-z0-9]+)\.md(?:$|[?#])/i);
  if (!match) {
    return "";
  }

  return references.get(match[1].toLowerCase())?.href || "";
}
