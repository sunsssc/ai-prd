export default function VibeHubLauncher() {
  return (
    <a
      className="vibe-hub-launcher"
      href="/hub/"
      aria-label="打开 Vibe App Hub"
      title="Vibe App Hub"
      target="_blank"
      rel="noopener noreferrer"
    >
      <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <circle cx="5.6" cy="5.6" r="1.7" />
        <circle cx="12" cy="5.6" r="1.7" />
        <circle cx="18.4" cy="5.6" r="1.7" />
        <circle cx="5.6" cy="12" r="1.7" />
        <circle cx="12" cy="12" r="1.7" />
        <circle cx="18.4" cy="12" r="1.7" />
        <circle cx="5.6" cy="18.4" r="1.7" />
        <circle cx="12" cy="18.4" r="1.7" />
        <circle cx="18.4" cy="18.4" r="1.7" />
      </svg>
    </a>
  );
}
