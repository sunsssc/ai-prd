const HTML_ARTIFACT_LANGUAGES = new Set(["html", "htm"]);
const SVG_ARTIFACT_LANGUAGES = new Set(["svg"]);

function normalizeLanguage(language = "") {
  return language.trim().toLowerCase();
}

function containsHtmlDocumentShell(source = "") {
  return /<!doctype\s+html[\s>]/i.test(source) || /<html[\s>]/i.test(source);
}

function containsRenderableHtml(source = "") {
  return /<(body|main|section|article|div|canvas|svg|style|script)[\s>]/i.test(source);
}

function containsPageBehavior(source = "") {
  return /<(style|script)[\s>]/i.test(source) || /<link\b[^>]*rel=["']?stylesheet/i.test(source);
}

const PREVIEW_FIT_STYLE = `<style data-ai-prd-preview-style>
  html {
    box-sizing: border-box;
  }

  *,
  *::before,
  *::after {
    box-sizing: inherit;
  }

  body {
    margin: 0;
    min-height: 100vh;
  }

  body[data-ai-prd-preview-fitted] {
    display: block !important;
    margin: 0 !important;
    min-height: 100vh;
  }

  .ai-prd-artifact-preview-root {
    overflow: auto;
    min-height: 100vh;
    padding: 16px;
  }

  .ai-prd-artifact-preview-scale-shell {
    width: 100%;
    min-width: 0;
  }

  .ai-prd-artifact-preview-content {
    width: 100%;
    min-width: 0;
    transform-origin: top left;
  }

  .ai-prd-artifact-preview-content > :first-child {
    margin-top: 0;
  }
</style>`;

const PREVIEW_THEME_STYLE = `<style data-ai-prd-preview-theme>
  :root {
    --font-sans: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    --color-text-primary: #2c2c2a;
    --color-text-secondary: #5f5e5a;
    --color-text-tertiary: #8a8882;
    --color-background-primary: #fffdf8;
    --color-background-secondary: #f8f3eb;
    --color-border-primary: #b8aa95;
    --color-border-secondary: #d8cbb8;
    --color-border-tertiary: #e6dccd;
  }
</style>`;

const SVG_ARTIFACT_STYLE_CONTENT = `
  :where(svg .t:not([fill])) {
    font: 400 14px var(--font-sans);
    fill: var(--color-text-primary);
  }

  :where(svg .th:not([fill])) {
    font: 600 14px var(--font-sans);
    fill: var(--color-text-primary);
  }

  :where(svg .ts:not([fill])) {
    font: 400 12px var(--font-sans);
    fill: var(--color-text-secondary);
  }

  :where(svg .box rect:not([fill]), svg rect.box:not([fill])) {
    fill: var(--color-background-secondary);
    stroke: var(--color-border-secondary);
    stroke-width: 0.75;
  }

  :where(svg .c-teal rect:not([fill]), svg .c-teal circle:not([fill]), svg .c-teal ellipse:not([fill])) {
    fill: #e1f5ee;
    stroke: #0f6e56;
    stroke-width: 0.75;
  }

  :where(svg .c-teal .th:not([fill])) {
    fill: #085041;
  }

  :where(svg .c-teal .ts:not([fill])) {
    fill: #0f6e56;
  }

  :where(svg .c-purple rect:not([fill]), svg .c-purple circle:not([fill]), svg .c-purple ellipse:not([fill])) {
    fill: #eeedfe;
    stroke: #534ab7;
    stroke-width: 0.75;
  }

  :where(svg .c-purple .th:not([fill])) {
    fill: #3c3489;
  }

  :where(svg .c-purple .ts:not([fill])) {
    fill: #534ab7;
  }

  :where(svg .c-coral rect:not([fill]), svg .c-coral circle:not([fill]), svg .c-coral ellipse:not([fill])) {
    fill: #faece7;
    stroke: #993c1d;
    stroke-width: 0.75;
  }

  :where(svg .c-coral .th:not([fill])) {
    fill: #712b13;
  }

  :where(svg .c-coral .ts:not([fill])) {
    fill: #993c1d;
  }

  :where(svg .c-pink rect:not([fill]), svg .c-pink circle:not([fill]), svg .c-pink ellipse:not([fill])) {
    fill: #fcebf2;
    stroke: #a8326a;
    stroke-width: 0.75;
  }

  :where(svg .c-pink .th:not([fill])) {
    fill: #7a1f4a;
  }

  :where(svg .c-pink .ts:not([fill])) {
    fill: #a8326a;
  }

  :where(svg .c-red rect:not([fill]), svg .c-red circle:not([fill]), svg .c-red ellipse:not([fill])) {
    fill: #fcebeb;
    stroke: #a32d2d;
    stroke-width: 0.75;
  }

  :where(svg .c-red .th:not([fill])) {
    fill: #791f1f;
  }

  :where(svg .c-red .ts:not([fill])) {
    fill: #a32d2d;
  }

  :where(svg .c-gray rect:not([fill]), svg .c-gray circle:not([fill]), svg .c-gray ellipse:not([fill])) {
    fill: #f1efe8;
    stroke: #b4b2a9;
    stroke-width: 0.75;
  }

  :where(svg .c-gray .th:not([fill])) {
    fill: #2c2c2a;
  }

  :where(svg .c-gray .ts:not([fill])) {
    fill: #5f5e5a;
  }
`;

const SVG_ARTIFACT_STYLE = `<style data-ai-prd-svg-artifact-style="true">${SVG_ARTIFACT_STYLE_CONTENT}</style>`;

const PREVIEW_FIT_SCRIPT = `<script data-ai-prd-preview-script>
(() => {
  const scriptElement = document.currentScript;
  let root = null;
  let scaleShell = null;
  let content = null;
  let frameId = 0;
  let currentScale = 1;

  function resolvePreviewShell() {
    root = document.querySelector("[data-ai-prd-preview-fit]");
    scaleShell = root ? root.querySelector(".ai-prd-artifact-preview-scale-shell") : null;
    content = root ? root.querySelector(".ai-prd-artifact-preview-content") : null;

    if (root && scaleShell && content) {
      document.body.dataset.aiPrdPreviewFitted = "true";
      return true;
    }

    root = document.createElement("div");
    scaleShell = document.createElement("div");
    content = document.createElement("div");

    root.className = "ai-prd-artifact-preview-root";
    root.dataset.aiPrdPreviewFit = "true";
    scaleShell.className = "ai-prd-artifact-preview-scale-shell";
    content.className = "ai-prd-artifact-preview-content";

    const visualNodes = Array.from(document.body.childNodes).filter((node) => {
      if (node === scriptElement) {
        return false;
      }

      if (node.nodeType === Node.ELEMENT_NODE) {
        const tagName = node.tagName.toLowerCase();
        return tagName !== "script" && tagName !== "style";
      }

      return Boolean(node.textContent.trim());
    });

    for (const node of visualNodes) {
      content.append(node);
    }

    scaleShell.append(content);
    root.append(scaleShell);
    document.body.dataset.aiPrdPreviewFitted = "true";
    document.body.insertBefore(root, document.body.firstChild);

    return true;
  }

  function measureVisibleContent() {
    const children = Array.from(content.children).filter((child) => {
      const tagName = child.tagName.toLowerCase();
      return tagName !== "script" && tagName !== "style";
    });

    if (!children.length) {
      return null;
    }

    const contentRect = content.getBoundingClientRect();
    let left = Infinity;
    let right = -Infinity;
    let top = Infinity;
    let bottom = -Infinity;

    for (const child of children) {
      const rect = child.getBoundingClientRect();
      if (!rect.width && !rect.height) {
        continue;
      }

      left = Math.min(left, (rect.left - contentRect.left) / currentScale);
      right = Math.max(right, (rect.right - contentRect.left) / currentScale);
      top = Math.min(top, (rect.top - contentRect.top) / currentScale);
      bottom = Math.max(bottom, (rect.bottom - contentRect.top) / currentScale);
    }

    if (!Number.isFinite(left) || !Number.isFinite(right)) {
      return null;
    }

    return {
      width: Math.ceil(right - left),
      height: Math.ceil(bottom - top)
    };
  }

  function fitPreview() {
    frameId = 0;

    const rootStyle = window.getComputedStyle(root);
    const horizontalPadding = parseFloat(rootStyle.paddingLeft) + parseFloat(rootStyle.paddingRight);
    const availableWidth = Math.max(0, root.clientWidth - horizontalPadding);
    const contentBounds = measureVisibleContent();

    if (!contentBounds || !contentBounds.width || contentBounds.width >= availableWidth) {
      currentScale = 1;
      content.style.width = "100%";
      content.style.transform = "none";
      scaleShell.style.height = "";
      return;
    }

    const scale = availableWidth / contentBounds.width;
    currentScale = scale;
    content.style.width = contentBounds.width + "px";
    content.style.transform = "scale(" + scale + ")";
    scaleShell.style.height = Math.ceil(contentBounds.height * scale) + "px";
  }

  function scheduleFitPreview() {
    if (frameId) {
      return;
    }

    frameId = window.requestAnimationFrame(fitPreview);
  }

  function startPreviewFit() {
    if (!resolvePreviewShell()) {
      return;
    }

    window.addEventListener("resize", scheduleFitPreview);
    new ResizeObserver(scheduleFitPreview).observe(root);
    new MutationObserver(scheduleFitPreview).observe(content, { childList: true, subtree: true });
    scheduleFitPreview();
  }

  if (document.readyState === "complete") {
    startPreviewFit();
  } else {
    window.addEventListener("load", startPreviewFit, { once: true });
  }
})();
</script>`;

function insertBeforeClosingTag(source, tagName, insertion) {
  const closingTagPattern = new RegExp(`</${tagName}\\s*>`, "i");
  if (closingTagPattern.test(source)) {
    return source.replace(closingTagPattern, `${insertion}\n$&`);
  }

  return `${source}\n${insertion}`;
}

function buildPreviewStyles({ includeSvgFallback = false } = {}) {
  return [PREVIEW_FIT_STYLE, PREVIEW_THEME_STYLE, includeSvgFallback ? SVG_ARTIFACT_STYLE : ""]
    .filter(Boolean)
    .join("\n");
}

function injectHtmlPreviewChrome(source, { includeSvgFallback = false } = {}) {
  const previewStyles = buildPreviewStyles({ includeSvgFallback });
  let withStyle = source;

  if (/<\/head\s*>/i.test(source)) {
    withStyle = insertBeforeClosingTag(source, "head", previewStyles);
  } else if (/<body\b[^>]*>/i.test(source)) {
    withStyle = source.replace(/<body\b[^>]*>/i, `$&\n${previewStyles}`);
  } else {
    withStyle = `${previewStyles}\n${source}`;
  }

  return insertBeforeClosingTag(withStyle, "body", PREVIEW_FIT_SCRIPT);
}

export function normalizeSvgArtifactSource(source = "") {
  const trimmedSource = source.trim();
  if (!/^<svg[\s>]/i.test(trimmedSource) || /data-ai-prd-svg-artifact-style/i.test(trimmedSource)) {
    return source;
  }

  return trimmedSource.replace(
    /<svg\b[^>]*>/i,
    (openingTag) => `${openingTag}\n<style data-ai-prd-svg-artifact-style="true">${SVG_ARTIFACT_STYLE_CONTENT}</style>`
  );
}

export function looksLikeHtmlArtifact(source = "", language = "") {
  const trimmedSource = source.trim();
  const normalizedLanguage = normalizeLanguage(language);
  if (!trimmedSource) {
    return false;
  }

  if (containsHtmlDocumentShell(trimmedSource)) {
    return true;
  }

  if (SVG_ARTIFACT_LANGUAGES.has(normalizedLanguage)) {
    return /<svg[\s>]/i.test(trimmedSource);
  }

  return (
    HTML_ARTIFACT_LANGUAGES.has(normalizedLanguage) &&
    containsRenderableHtml(trimmedSource) &&
    containsPageBehavior(trimmedSource)
  );
}

export function buildHtmlPreviewDocument(source = "") {
  const trimmedSource = normalizeSvgArtifactSource(source).trim();
  const includeSvgFallback = /<svg[\s>]/i.test(trimmedSource);
  if (containsHtmlDocumentShell(trimmedSource)) {
    return injectHtmlPreviewChrome(trimmedSource, { includeSvgFallback });
  }

  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HTML 预览</title>
  ${buildPreviewStyles({ includeSvgFallback })}
</head>
<body>
<div class="ai-prd-artifact-preview-root" data-ai-prd-preview-fit>
<div class="ai-prd-artifact-preview-scale-shell">
<div class="ai-prd-artifact-preview-content">
${trimmedSource}
</div>
</div>
</div>
${PREVIEW_FIT_SCRIPT}
</body>
</html>`;
}
