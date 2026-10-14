import plantumlEncoder from "plantuml-encoder";

const DIAGRAM_BLOCK_PATTERN = /```(mermaid|plantuml)\s*\n([\s\S]*?)\n```/gi;
const CLICKUP_DIAGRAM_PATTERN = /(?:!\[[^\]]*\]\([^\n)]+\)\s*\n\s*)?<!-- doco-clickup-diagram:(mermaid|plantuml):v1 -->\s*\n```(?:mermaid|plantuml)\s*\n([\s\S]*?)\n```/gi;
const CLICKUP_SPREADSHEET_PATTERN = /<!-- doco-clickup-spreadsheet:v1 -->\s*\n((?:\|[^\n]*\|\s*(?:\n|$))+)/gi;
const CSV_BLOCK_PATTERN = /```csv\s*\n([\s\S]*?)\n```/gi;
const EDITOR_BLANK_LINE_SENTINEL = "\u200B";

// Doco's Markdown parser can close a fenced block early when its body has a
// truly empty line followed by a Markdown heading. Keep those empty lines
// visually empty in the editor and restore them before saving to ClickUp.
const FENCED_BLOCK_PATTERN = /(^|\n)(`{3,}|~{3,})([^\n]*)\n([\s\S]*?)\n\2(?=\n|$)/g;

const DIAGRAM_LABELS = {
  mermaid: "Mermaid 流程图",
  plantuml: "PlantUML UML 图"
};

function encodeBase64Url(value) {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function buildMermaidImageUrl(source) {
  const payload = JSON.stringify({
    code: source,
    mermaid: { theme: "default" }
  });
  return `https://mermaid.ink/img/${encodeBase64Url(payload)}`;
}

function buildPlantUmlImageUrl(source) {
  return `https://www.plantuml.com/plantuml/png/${plantumlEncoder.encode(source)}`;
}

function parseCsvRows(csv) {
  const rows = [];
  let row = [];
  let cell = "";
  let quoted = false;

  for (let index = 0; index < csv.length; index += 1) {
    const character = csv[index];
    const nextCharacter = csv[index + 1];
    if (character === '"' && quoted && nextCharacter === '"') {
      cell += '"';
      index += 1;
    } else if (character === '"') {
      quoted = !quoted;
    } else if (character === "," && !quoted) {
      row.push(cell);
      cell = "";
    } else if ((character === "\n" || character === "\r") && !quoted) {
      if (character === "\r" && nextCharacter === "\n") index += 1;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else {
      cell += character;
    }
  }

  if (cell || row.length > 0) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

function escapeTableCell(value) {
  return String(value ?? "")
    .replace(/\\/g, "\\\\")
    .replace(/\|/g, "\\|")
    .replace(/\r?\n/g, "<br>");
}

function csvToMarkdownTable(csv) {
  const rows = parseCsvRows(csv.replace(/\r\n/g, "\n").replace(/\n$/, ""));
  const width = Math.max(1, ...rows.map((row) => row.length));
  const normalizedRows = rows.length > 0 ? rows : [[]];
  const lines = normalizedRows.map((row) => (
    `| ${Array.from({ length: width }, (_, index) => escapeTableCell(row[index])).join(" | ")} |`
  ));
  const separator = `| ${Array.from({ length: width }, () => "---").join(" | ")} |`;
  if (lines.length === 1) return `${lines[0]}\n${separator}`;
  return [lines[0], separator, ...lines.slice(1)].join("\n");
}

function parseMarkdownTableRow(line) {
  const cells = [];
  let cell = "";
  let escaped = false;
  for (let index = 1; index < line.length - 1; index += 1) {
    const character = line[index];
    if (escaped) {
      cell += character;
      escaped = false;
    } else if (character === "\\") {
      escaped = true;
    } else if (character === "|") {
      cells.push(cell.trim().replace(/<br>/g, "\n"));
      cell = "";
    } else {
      cell += character;
    }
  }
  if (escaped) cell += "\\";
  cells.push(cell.trim().replace(/<br>/g, "\n"));
  return cells;
}

function markdownTableToCsv(table) {
  const rows = table
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.startsWith("|") && line.endsWith("|"))
    .map(parseMarkdownTableRow);
  if (rows.length < 2) return "";
  const dataRows = rows.filter((row) => !row.every((cell) => /^:?-{3,}:?$/.test(cell)));
  return dataRows
    .map((row) => row.map((cell) => {
      const value = cell.replace(/"/g, '""');
      return /[",\n]/.test(value) ? `"${value}"` : value;
    }).join(","))
    .join("\n");
}

function normalizeMarkdownTableSeparators(markdown) {
  return markdown.replace(/^\|(?:\s*:?-{3,}:?\s*\|)+\s*$/gm, (line) => {
    const cells = parseMarkdownTableRow(line);
    return `| ${cells.join(" | ")} |`;
  });
}

function restoreEditorDiagrams(markdown) {
  return markdown.replace(CLICKUP_DIAGRAM_PATTERN, (_, type, source) => `\`\`\`${type}\n${source}\n\`\`\``);
}

function restoreEditorSpreadsheets(markdown) {
  return markdown.replace(CLICKUP_SPREADSHEET_PATTERN, (_, table) => {
    const csv = markdownTableToCsv(table);
    return csv ? `\`\`\`csv\n${csv}\n\`\`\`` : table;
  });
}

function protectEditorCodeFenceBlankLines(markdown) {
  return markdown.replace(FENCED_BLOCK_PATTERN, (_, prefix, fence, info, body) => (
    `${prefix}${fence}${info}\n${body
      .split("\n")
      .map((line) => line || EDITOR_BLANK_LINE_SENTINEL)
      .join("\n")}\n${fence}`
  ));
}

function restoreEditorCodeFenceBlankLines(markdown) {
  return String(markdown || "").replaceAll(EDITOR_BLANK_LINE_SENTINEL, "");
}

export function clickUpMarkdownToEditorMarkdown(markdown) {
  const restored = restoreEditorSpreadsheets(restoreEditorDiagrams(String(markdown || "")));
  return protectEditorCodeFenceBlankLines(restored);
}

function serializeDiagram(type, source) {
  const normalizedSource = source.replace(/\r\n/g, "\n").trim();
  const imageUrl = type === "mermaid"
    ? buildMermaidImageUrl(normalizedSource)
    : buildPlantUmlImageUrl(normalizedSource);
  const label = DIAGRAM_LABELS[type];
  return [
    `![${label}](${imageUrl})`,
    `<!-- doco-clickup-diagram:${type}:v1 -->`,
    `\`\`\`${type}`,
    normalizedSource,
    "```"
  ].join("\n");
}

function serializeSpreadsheet(csv) {
  return [
    "<!-- doco-clickup-spreadsheet:v1 -->",
    csvToMarkdownTable(csv)
  ].join("\n");
}

export function editorMarkdownToClickUpMarkdown(markdown) {
  let converted = String(markdown || "").replace(DIAGRAM_BLOCK_PATTERN, (_, type, source) => serializeDiagram(type.toLowerCase(), source));
  converted = converted.replace(CSV_BLOCK_PATTERN, (_, csv) => serializeSpreadsheet(csv));
  return normalizeMarkdownTableSeparators(restoreEditorCodeFenceBlankLines(converted));
}

export const clickupContentAdapterInternals = {
  buildMermaidImageUrl,
  buildPlantUmlImageUrl,
  csvToMarkdownTable,
  markdownTableToCsv,
  normalizeMarkdownTableSeparators,
  parseCsvRows
};
