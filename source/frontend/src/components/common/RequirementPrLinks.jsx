import Badge from "./Badge";
import { prStatusMeta, shortRepoName } from "../../utils/requirementPrLinks";

export default function RequirementPrLinks({ pullRequests }) {
  if (!pullRequests || pullRequests.length === 0) {
    return null;
  }

  return (
    <>
      <div className="pr-popover-head">
        <span className="pr-popover-head-label">关联 PR</span>
        <span className="pr-popover-count">{pullRequests.length}</span>
      </div>
      <ul className="requirement-pr-link-list">
        {pullRequests.map((pr) => {
          const status = prStatusMeta(pr.state);
          return (
            <li key={`${pr.repo_full_name}#${pr.pr_number}-${pr.task_id}`}>
              <a
                className="requirement-pr-link-row"
                href={pr.pr_url}
                target="_blank"
                rel="noreferrer"
                title={`作者：${pr.author_login || "未知"} · 目标分支：${pr.base_ref || "未知"}`}
              >
                <span className="requirement-pr-link-top">
                  <span className="requirement-pr-link-repo">
                    {shortRepoName(pr.repo_full_name)}
                    <span className="requirement-pr-link-number">#{pr.pr_number}</span>
                  </span>
                  <Badge tone={status.tone}>{status.label}</Badge>
                </span>
                <span className="requirement-pr-link-title">{pr.title}</span>
              </a>
            </li>
          );
        })}
      </ul>
    </>
  );
}
