import { looksLikeHtmlArtifact } from "./htmlArtifacts.js";

export const sessionVisualizationDisplayText = "可视化以上会话";
export const turnVisualizationDisplayText = "可视化本轮问答";

const markerPrefix = "<!-- ai-prd-visualization-request:";
const markerSuffix = " -->";
const visualizationCodeFencePattern = /(^|\n)```(?:svg|html|htm|mermaid)\b/i;
const visualizationCodeFenceBlockPattern = /(^|\n)```(?:svg|html|htm|mermaid)\b[^\n]*\n[\s\S]*?```/i;
const firstRawSvgBlockPattern = /<svg\b[\s\S]*?<\/svg>/i;
const htmlDocumentPattern = /(?:<!doctype\s+html[\s\S]*?)?<html[\s\S]*?<\/html>/i;
const rawSvgDocumentPattern = /^<svg\b[\s\S]*<\/svg>\s*$/i;
const skillPromptLeakPatterns = [
  /Base directory for this skill:/i,
  /(^|\n)---\s*\nname:\s*[-_\w]+\b/i,
  /#\s*[\w -]+ Skill/i,
  /Core Design System/i,
  /Hard prohibitions/i,
  /Color assignment rules/i,
  /The 680 in viewBox is load-bearing/i,
  /Anthropic Sans/i,
  /sendPrompt\(text\)/i,
  /c-\{ramp\}/i,
  /Step 0\s*[—-]\s*Choose output type/i
];

export function buildVisualizationRequestContent(request, prompt) {
  return `${markerPrefix}${encodeURIComponent(JSON.stringify(request))}${markerSuffix}\n${prompt}`;
}

export function parseVisualizationRequest(content) {
  if (typeof content !== "string") {
    return null;
  }

  const match = content.match(/^\s*<!-- ai-prd-visualization-request:([^]*?) -->/);
  if (!match) {
    return null;
  }

  try {
    const request = JSON.parse(decodeURIComponent(match[1]));
    if (request?.scope === "session") {
      return { scope: "session" };
    }
    if (request?.scope === "turn" && (request.targetTurnId || request.targetMessageId)) {
      return {
        scope: "turn",
        targetTurnId: request.targetTurnId ? String(request.targetTurnId) : null,
        targetMessageId: request.targetMessageId ? String(request.targetMessageId) : null
      };
    }
  } catch {
    return null;
  }

  return null;
}

function findSkillPromptLeakIndex(content) {
  const matches = skillPromptLeakPatterns
    .map((pattern) => pattern.exec(content))
    .filter(Boolean)
    .map((match) => match.index);

  if (!matches.length) {
    return -1;
  }

  const strongLeak = skillPromptLeakPatterns.slice(0, 3).some((pattern) => pattern.test(content));
  if (!strongLeak && matches.length < 2) {
    return -1;
  }

  return Math.min(...matches);
}

function findVisualizationCodeFenceIndex(content, fromIndex = 0) {
  const slicedContent = content.slice(Math.max(0, fromIndex));
  const match = visualizationCodeFencePattern.exec(slicedContent);
  if (!match) {
    return -1;
  }

  return fromIndex + match.index + (slicedContent[match.index] === "\n" ? 1 : 0);
}

export function sanitizeSkillPromptLeak(markdown, { preferVisualizationCode = false } = {}) {
  if (typeof markdown !== "string" || !markdown.trim()) {
    return markdown || "";
  }

  const leakIndex = findSkillPromptLeakIndex(markdown);
  if (leakIndex < 0) {
    return markdown;
  }

  const codeFenceIndex = findVisualizationCodeFenceIndex(markdown, leakIndex);
  if (preferVisualizationCode) {
    return codeFenceIndex >= 0 ? markdown.slice(codeFenceIndex).trimStart() : "";
  }

  const prefix = markdown.slice(0, leakIndex).trimEnd();
  if (codeFenceIndex < 0) {
    return prefix;
  }

  return [prefix, markdown.slice(codeFenceIndex).trimStart()].filter(Boolean).join("\n\n");
}

export function sanitizeVisualizationMarkdown(markdown) {
  return sanitizeSkillPromptLeak(markdown, { preferVisualizationCode: true });
}

function wrapArtifactCodeBlock(language, source) {
  return `\`\`\`${language}\n${source.trim()}\n\`\`\``;
}

function extractFirstVisualizationCodeFence(source = "") {
  const match = visualizationCodeFenceBlockPattern.exec(source);
  return match ? match[0].trim() : "";
}

function extractFirstRawSvgBlock(source = "") {
  const match = firstRawSvgBlockPattern.exec(source);
  return match ? match[0].trim() : "";
}

function extractFirstHtmlDocument(source = "") {
  const match = htmlDocumentPattern.exec(source);
  return match ? match[0].trim() : "";
}

export function prepareVisualizationMarkdown(markdown) {
  const sanitized = sanitizeVisualizationMarkdown(markdown);
  const trimmed = sanitized.trim();

  if (!trimmed) {
    return sanitized;
  }

  const visualizationCodeFence = extractFirstVisualizationCodeFence(sanitized);
  if (visualizationCodeFence) {
    return visualizationCodeFence;
  }

  const visualizationCodeFenceIndex = findVisualizationCodeFenceIndex(trimmed);
  if (visualizationCodeFenceIndex >= 0) {
    return trimmed.slice(visualizationCodeFenceIndex).trim();
  }

  if (rawSvgDocumentPattern.test(trimmed)) {
    return wrapArtifactCodeBlock("svg", trimmed);
  }

  const rawSvgBlock = extractFirstRawSvgBlock(trimmed);
  if (rawSvgBlock) {
    return wrapArtifactCodeBlock("svg", rawSvgBlock);
  }

  const htmlDocument = extractFirstHtmlDocument(trimmed);
  if (htmlDocument) {
    return wrapArtifactCodeBlock("html", htmlDocument);
  }

  if (looksLikeHtmlArtifact(trimmed, "html")) {
    return wrapArtifactCodeBlock("html", trimmed);
  }

  return "";
}
