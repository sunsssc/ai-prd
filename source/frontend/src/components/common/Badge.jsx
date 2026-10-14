export default function Badge({ tone = "neutral", children, subtle = false }) {
  return (
    <span className={`badge badge-${tone} ${subtle ? "badge-subtle" : ""}`.trim()}>
      {children}
    </span>
  );
}
