export default function ActionIcon({ kind, className = "message-action-icon" }) {
  const commonProps = {
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: "1.7",
    strokeLinecap: "round",
    strokeLinejoin: "round",
    className,
    "aria-hidden": "true"
  };

  if (kind === "copy") {
    return (
      <svg {...commonProps}>
        <rect x="9" y="9" width="10" height="10" rx="1.8" />
        <path d="M7 15H6a1 1 0 0 1-1-1V6.8A1.8 1.8 0 0 1 6.8 5H14a1 1 0 0 1 1 1v1" />
      </svg>
    );
  }

  if (kind === "copy-to-input") {
    return (
      <svg {...commonProps}>
        <path d="M8 5.5h8a2 2 0 0 1 2 2v4" />
        <path d="m15 9 3 3 3-3" />
        <rect x="4" y="14" width="14" height="5" rx="2" />
      </svg>
    );
  }

  if (kind === "check") {
    return (
      <svg {...commonProps}>
        <path d="m5.5 12.5 4.2 4.2L18.5 8" />
      </svg>
    );
  }

  if (kind === "thumb-up") {
    return (
      <svg {...commonProps}>
        <path d="M9.5 11V19H6.8A1.8 1.8 0 0 1 5 17.2v-4.4A1.8 1.8 0 0 1 6.8 11H9.5Z" />
        <path d="M9.5 11 13 5.7c.4-.6 1.2-.8 1.8-.4.5.3.8.8.8 1.4V11h2.6c1 0 1.8.8 1.8 1.8a2 2 0 0 1-.1.6l-1.7 5.2a1.8 1.8 0 0 1-1.7 1.2H9.5" />
      </svg>
    );
  }

  if (kind === "thumb-down") {
    return (
      <svg {...commonProps}>
        <path d="M9.5 13V5H6.8A1.8 1.8 0 0 0 5 6.8v4.4A1.8 1.8 0 0 0 6.8 13H9.5Z" />
        <path d="M9.5 13 13 18.3c.4.6 1.2.8 1.8.4.5-.3.8-.8.8-1.4V13h2.6c1 0 1.8-.8 1.8-1.8a2 2 0 0 0-.1-.6l-1.7-5.2a1.8 1.8 0 0 0-1.7-1.2H9.5" />
      </svg>
    );
  }

  if (kind === "download") {
    return (
      <svg {...commonProps}>
        <path d="M12 4v10" />
        <path d="m8.5 10.5 3.5 3.5 3.5-3.5" />
        <path d="M5 18.2A1.8 1.8 0 0 0 6.8 20h10.4A1.8 1.8 0 0 0 19 18.2V18" />
      </svg>
    );
  }

  if (kind === "diagram") {
    return (
      <svg {...commonProps}>
        <rect x="4" y="4.5" width="6" height="5" rx="1.3" />
        <rect x="14" y="4.5" width="6" height="5" rx="1.3" />
        <rect x="9" y="14.5" width="6" height="5" rx="1.3" />
        <path d="M10 7h4" />
        <path d="M7 9.5v2.2a2 2 0 0 0 2 2h3" />
        <path d="M17 9.5v2.2a2 2 0 0 1-2 2h-3" />
      </svg>
    );
  }

  if (kind === "chart") {
    return (
      <svg {...commonProps}>
        <path d="M5 19h14" />
        <rect x="6.2" y="11" width="3" height="6" rx="0.9" />
        <rect x="10.5" y="7.5" width="3" height="9.5" rx="0.9" />
        <rect x="14.8" y="13" width="3" height="4" rx="0.9" />
      </svg>
    );
  }

  if (kind === "maximize") {
    return (
      <svg {...commonProps}>
        <path d="M8.5 4.8H5.8a1 1 0 0 0-1 1v2.7" />
        <path d="M15.5 4.8h2.7a1 1 0 0 1 1 1v2.7" />
        <path d="M19.2 15.5v2.7a1 1 0 0 1-1 1h-2.7" />
        <path d="M8.5 19.2H5.8a1 1 0 0 1-1-1v-2.7" />
        <path d="M8.5 8.5 4.8 4.8" />
        <path d="m15.5 8.5 3.7-3.7" />
        <path d="m19.2 19.2-3.7-3.7" />
        <path d="m4.8 19.2 3.7-3.7" />
      </svg>
    );
  }

  if (kind === "minimize") {
    return (
      <svg {...commonProps}>
        <path d="M4.8 8.5h2.7a1 1 0 0 0 1-1V4.8" />
        <path d="M19.2 8.5h-2.7a1 1 0 0 1-1-1V4.8" />
        <path d="M15.5 19.2v-2.7a1 1 0 0 1 1-1h2.7" />
        <path d="M8.5 19.2v-2.7a1 1 0 0 0-1-1H4.8" />
        <path d="M8.5 8.5 5.2 5.2" />
        <path d="m15.5 8.5 3.3-3.3" />
        <path d="m15.5 15.5 3.3 3.3" />
        <path d="m8.5 15.5-3.3 3.3" />
      </svg>
    );
  }

  if (kind === "close") {
    return (
      <svg {...commonProps}>
        <path d="m6 6 12 12" />
        <path d="M18 6 6 18" />
      </svg>
    );
  }

  if (kind === "send") {
    return (
      <svg {...commonProps}>
        <path d="M5 12 19 5l-4 14-3.5-5.5L5 12Z" />
        <path d="M11.5 13.5 19 5" />
      </svg>
    );
  }

  if (kind === "plus") {
    return (
      <svg {...commonProps}>
        <path d="M12 5v14" />
        <path d="M5 12h14" />
      </svg>
    );
  }

  if (kind === "stop") {
    return (
      <svg {...commonProps} fill="currentColor" stroke="none">
        <rect x="7" y="7" width="10" height="10" rx="2" />
      </svg>
    );
  }

  if (kind === "history") {
    return (
      <svg {...commonProps}>
        <path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1" />
        <path d="M3.5 4.5v4h4" />
        <path d="M12 7.5V12l3 1.8" />
      </svg>
    );
  }

  if (kind === "star") {
    return (
      <svg {...commonProps}>
        <path d="m12 3.7 2.6 5.2 5.7.8-4.1 4 .9 5.6-5.1-2.7-5.1 2.7.9-5.6-4.1-4 5.7-.8L12 3.7Z" />
      </svg>
    );
  }

  return (
    <svg {...commonProps}>
      <path d="M20 11a8 8 0 1 1-2.3-5.7" />
      <path d="M20 5v6h-6" />
    </svg>
  );
}
