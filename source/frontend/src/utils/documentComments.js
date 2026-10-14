function findSectionHeadings(markdown = "") {
  const headings = [];
  let offset = 0;
  let fenceMarker = "";

  for (const line of markdown.split("\n")) {
    const fenceMatch = line.match(/^\s*(`{3,}|~{3,})/);
    if (fenceMatch) {
      const marker = fenceMatch[1][0];
      if (!fenceMarker) {
        fenceMarker = marker;
      } else if (fenceMarker === marker) {
        fenceMarker = "";
      }
    } else if (!fenceMarker) {
      const headingMatch = line.match(/^##[ \t]+(.+?)[ \t]*$/);
      if (headingMatch) {
        headings.push({
          title: headingMatch[1].trim(),
          start: offset,
          end: offset + line.length
        });
      }
    }

    offset += line.length + 1;
  }

  return headings;
}

function parseQuotedComment(block = "") {
  const lines = block.split("\n").map((line) => line.replace(/^> ?/, ""));
  const header = lines[0]?.trim() || "";
  const headerMatch = header.match(/^\*\*(.+?)\*\*(?:\s*·\s*(.+))?$/);

  if (!headerMatch) {
    return {
      author: "",
      createdAt: "",
      content: lines.join("\n").trim()
    };
  }

  while (lines.length > 1 && !lines[1].trim()) {
    lines.splice(1, 1);
  }

  return {
    author: headerMatch[1].trim(),
    createdAt: (headerMatch[2] || "").trim(),
    content: lines.slice(1).join("\n").trim()
  };
}

function parseCommentBlocks(markdown = "") {
  const matches = markdown.match(/(?:^>[^\n]*(?:\n|$))+/gm) || [];
  const comments = matches
    .map((block) => parseQuotedComment(block.trimEnd()))
    .filter((comment) => comment.author || comment.createdAt || comment.content);

  if (comments.length || !markdown.trim()) {
    return comments;
  }

  return [{ author: "", createdAt: "", content: markdown.trim() }];
}

export function splitDocumentComments(markdown = "") {
  const normalized = markdown.replace(/\r\n/g, "\n");
  const headings = findSectionHeadings(normalized);
  const commentHeadingIndex = headings.findIndex((heading) => heading.title === "评论");

  if (commentHeadingIndex < 0) {
    return { content: normalized, comments: [] };
  }

  const commentHeading = headings[commentHeadingIndex];
  const nextHeading = headings[commentHeadingIndex + 1];
  const sectionEnd = nextHeading?.start ?? normalized.length;
  const commentMarkdown = normalized
    .slice(commentHeading.end, sectionEnd)
    .replace(/^\n+|\n+$/g, "");
  const content = [
    normalized.slice(0, commentHeading.start).trimEnd(),
    normalized.slice(sectionEnd).trimStart()
  ]
    .filter(Boolean)
    .join("\n\n");

  return {
    content,
    comments: parseCommentBlocks(commentMarkdown)
  };
}
