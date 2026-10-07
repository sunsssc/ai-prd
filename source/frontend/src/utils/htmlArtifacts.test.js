import assert from "node:assert/strict";
import test from "node:test";
import { buildHtmlPreviewDocument, looksLikeHtmlArtifact, normalizeSvgArtifactSource } from "./htmlArtifacts.js";

test("完整 HTML 文档会被识别为可预览 Artifact", () => {
  const source = `<!DOCTYPE html>
<html lang="zh-CN">
<head><style>body { margin: 0; }</style></head>
<body><canvas id="chart"></canvas><script>window.ready = true;</script></body>
</html>`;
  const previewDocument = buildHtmlPreviewDocument(source);

  assert.equal(looksLikeHtmlArtifact(source, ""), true);
  assert.notEqual(previewDocument, source);
  assert.match(previewDocument, /data-ai-prd-preview-style/);
  assert.match(previewDocument, /data-ai-prd-preview-script/);
  assert.match(previewDocument, /data-ai-prd-preview-fit/);
  assert.match(previewDocument, /<canvas id="chart"><\/canvas>/);
});

test("带样式或脚本的 html 片段会被包装成可预览文档", () => {
  const source = `<style>main { color: red; }</style><main>可视化效果</main>`;
  const previewDocument = buildHtmlPreviewDocument(source);

  assert.equal(looksLikeHtmlArtifact(source, "html"), true);
  assert.match(previewDocument, /^<!doctype html>/);
  assert.match(previewDocument, /<meta name="viewport"/);
  assert.match(previewDocument, /ai-prd-artifact-preview-root/);
  assert.match(previewDocument, /data-ai-prd-preview-fit/);
  assert.match(previewDocument, /ai-prd-artifact-preview-scale-shell/);
  assert.match(previewDocument, /width: 100%/);
  assert.match(previewDocument, /<main>可视化效果<\/main>/);
});

test("svg 代码块会被当成可预览 Artifact", () => {
  const source = `<svg width="100%" viewBox="0 0 680 160" role="img"><title>可视化</title></svg>`;
  const previewDocument = buildHtmlPreviewDocument(source);

  assert.equal(looksLikeHtmlArtifact(source, "svg"), true);
  assert.match(previewDocument, /^<!doctype html>/);
  assert.match(previewDocument, /<svg width="100%"/);
  assert.match(previewDocument, /data-ai-prd-preview-theme/);
  assert.match(previewDocument, /data-ai-prd-svg-artifact-style="true"/);
});

test("svg 预览为缺失内联样式的 visualizer 色彩类提供兜底", () => {
  const source = [
    `<svg width="100%" viewBox="0 0 680 160" role="img">`,
    `<g class="c-coral">`,
    `<rect x="40" y="32" width="182" height="66" rx="6"/>`,
    `<text x="131" y="53" class="th" text-anchor="middle">Business 枚举</text>`,
    `</g>`,
    `</svg>`
  ].join("");
  const previewDocument = buildHtmlPreviewDocument(source);
  const normalizedSource = normalizeSvgArtifactSource(source);

  assert.match(previewDocument, /:where\(svg \.c-coral rect:not\(\[fill\]\)/);
  assert.match(previewDocument, /fill: #faece7/);
  assert.match(previewDocument, /--color-text-secondary: #5f5e5a/);
  assert.match(normalizedSource, /data-ai-prd-svg-artifact-style="true"/);
  assert.match(normalizedSource, /fill: #faece7/);
});

test("普通 HTML 示例片段不会被当成 Artifact", () => {
  assert.equal(looksLikeHtmlArtifact("<button>保存</button>", "html"), false);
  assert.equal(looksLikeHtmlArtifact("console.log('ok')", "javascript"), false);
});
