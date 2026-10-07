import Badge from "./Badge";

function formatTimestampToSecond(value = "") {
  const normalized = value
    .trim()
    .replace("T", " ")
    .replace(/\.\d+/, "")
    .replace(/(?:Z|[+-]\d{2}:\d{2})$/i, "")
    .trim();

  if (!normalized) {
    return "";
  }

  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(normalized)) {
    return normalized;
  }

  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(normalized)) {
    return `${normalized}:00`;
  }

  return normalized;
}

export default function PreviewToolbar({ timestamp = "", actions = null }) {
  const formattedTimestamp = formatTimestampToSecond(timestamp);

  if (!formattedTimestamp && !actions) {
    return null;
  }

  return (
    <div className="detail-card detail-toolbar-card">
      <div className="detail-head">
        {formattedTimestamp ? (
          <div className="detail-meta">
            <Badge tone="neutral" subtle>
              {formattedTimestamp}
            </Badge>
          </div>
        ) : (
          <div />
        )}
        <div className="content-actions">{actions}</div>
      </div>
    </div>
  );
}
