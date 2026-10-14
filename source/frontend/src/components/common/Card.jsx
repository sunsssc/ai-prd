export default function Card({ className = "", children }) {
  return <section className={`card ${className}`.trim()}>{children}</section>;
}
