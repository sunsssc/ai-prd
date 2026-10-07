import Badge from "./Badge";

export default function PageHeader({ title, subtitle, badges = [], actions = [] }) {
  return (
    <header className="page-header">
      <div className="page-header__top">
        <div>
          <h1 className="page-title">{title}</h1>
          <p className="page-subtitle">{subtitle}</p>
        </div>
        <div className="badge-row">
          {badges.map((badge) => (
            <Badge key={badge.label} tone={badge.tone}>
              {badge.label}
            </Badge>
          ))}
        </div>
      </div>

      {actions.length > 0 ? (
        <div className="page-header__actions">
          {actions.map((action) => (
            <a key={action.label} className="text-link" href={action.href}>
              {action.label}
            </a>
          ))}
        </div>
      ) : null}
    </header>
  );
}
