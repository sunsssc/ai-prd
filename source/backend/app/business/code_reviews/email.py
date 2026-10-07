from __future__ import annotations

import html as html_lib
import re
from typing import Protocol

import markdown


class CodeReviewEmailReview(Protocol):
    repo_full_name: str
    pr_number: int
    pr_url: str
    author_login: str
    requested_by_login: str
    requested_reviewer_logins: tuple[str, ...]
    requested_team_slugs: tuple[str, ...]
    risk_level: str
    content: str


def build_email_body(review: CodeReviewEmailReview) -> str:
    recipient_name = review.author_login or "同学"
    trigger_reason = _format_review_request_reason(review)
    return "\n".join(
        [
            f"你好，{recipient_name}：",
            "",
            "我是 ai-prd 的代码审查 agent。",
            trigger_reason,
            "",
            f"仓库：{review.repo_full_name}",
            f"PR：#{review.pr_number}",
            f"链接：{review.pr_url}",
            f"PR 提交人：{review.author_login or '-'}",
            f"触发人：{review.requested_by_login or '-'}",
            f"风险等级：{review.risk_level}",
            "",
            "以下是 AI 自动审核意见：",
            "",
            review.content,
            "",
            "请以实际代码和项目上下文为准，AI 结论只作为辅助审查意见。",
        ]
    )


def build_email_html_body(review: CodeReviewEmailReview) -> str:
    recipient_name = html_lib.escape(review.author_login or "同学")
    trigger_reason = html_lib.escape(_format_review_request_reason(review))
    repo_full_name = html_lib.escape(review.repo_full_name)
    pr_number = html_lib.escape(str(review.pr_number))
    pr_url = html_lib.escape(review.pr_url, quote=True)
    author_login = html_lib.escape(review.author_login or "-")
    requested_by_login = html_lib.escape(review.requested_by_login or "-")
    risk_level = html_lib.escape(review.risk_level)
    rendered_review = render_markdown_for_email(review.content)
    return f"""<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#f6f8fa;color:#24292f;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,'PingFang SC','Microsoft YaHei',sans-serif;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;background:#f6f8fa;margin:0;padding:24px 0;">
      <tr>
        <td align="center" style="padding:0 12px;">
          <table role="presentation" width="680" cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;max-width:680px;background:#ffffff;border:1px solid #d8dee4;border-radius:8px;">
            <tr>
              <td style="padding:28px 32px 20px 32px;border-bottom:1px solid #d8dee4;">
                <div style="font-size:13px;line-height:20px;color:#57606a;margin:0 0 8px 0;">ai-prd code review agent</div>
                <h1 style="font-size:22px;line-height:30px;color:#24292f;margin:0;font-weight:700;">AI 自动代码审核已完成</h1>
              </td>
            </tr>
            <tr>
              <td style="padding:24px 32px 8px 32px;font-size:15px;line-height:24px;color:#24292f;">
                <p style="margin:0 0 12px 0;">你好，{recipient_name}：</p>
                <p style="margin:0 0 12px 0;">我是 ai-prd 的代码审查 agent。{trigger_reason}</p>
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin:18px 0;background:#f6f8fa;border:1px solid #d8dee4;border-radius:6px;">
                  <tr>
                    <td style="padding:14px 16px;font-size:14px;line-height:22px;color:#24292f;">
                      <div style="margin:0 0 6px 0;"><strong>仓库：</strong>{repo_full_name}</div>
                      <div style="margin:0 0 6px 0;"><strong>PR：</strong><a href="{pr_url}" style="color:#0969da;text-decoration:none;">#{pr_number}</a></div>
                      <div style="margin:0 0 6px 0;"><strong>PR 提交人：</strong>{author_login}</div>
                      <div style="margin:0 0 6px 0;"><strong>触发人：</strong>{requested_by_login}</div>
                      <div style="margin:0;"><strong>风险等级：</strong>{risk_level}</div>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td style="padding:0 32px 24px 32px;">
                <div style="font-size:16px;line-height:24px;font-weight:700;color:#24292f;margin:0 0 12px 0;">AI 自动审核意见</div>
                <div style="font-size:14px;line-height:22px;color:#24292f;">
                  {rendered_review}
                </div>
              </td>
            </tr>
            <tr>
              <td style="padding:18px 32px;border-top:1px solid #d8dee4;font-size:12px;line-height:18px;color:#57606a;">
                请以实际代码和项目上下文为准，AI 结论只作为辅助审查意见。
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def render_markdown_for_email(markdown_text: str) -> str:
    escaped_markdown = markdown_text.strip().replace("&", "&amp;").replace("<", "&lt;")
    rendered = markdown.markdown(
        escaped_markdown,
        extensions=["fenced_code", "tables", "sane_lists", "nl2br"],
        output_format="html5",
    )
    return _inline_email_markdown_styles(rendered)


def _format_requested_targets(reviewer_logins: tuple[str, ...], team_slugs: tuple[str, ...]) -> str:
    reviewers = [f"@{login}" for login in reviewer_logins if login]
    teams = [f"team:{slug}" for slug in team_slugs if slug]
    targets = [*reviewers, *teams]
    return f"（{', '.join(targets)}）" if targets else ""


def _format_review_request_reason(review: CodeReviewEmailReview) -> str:
    requested_targets = _format_requested_targets(review.requested_reviewer_logins, review.requested_team_slugs)
    return f"你的 PR 已邀请审核人{requested_targets}，我自动触发并完成了一次 AI 代码审核。"


EMAIL_FINDING_TITLE_RE = re.compile(
    r'<li style="margin:0 0 6px 0;"><strong>((?:\[(?:高|中|低|P[0-3])\]|P[0-3]\s*[—-]|[高中低]风险\b)[\s\S]*?)</strong></li>'
)


def _style_email_finding_title(match: re.Match[str]) -> str:
    title_html = match.group(1)
    title_text = re.sub(r"<[^>]+>", "", title_html).strip()
    if title_text.startswith(("[高]", "[P0]", "[P1]", "P0", "P1", "高风险")):
        accent = "#b53333"
        background = "#fff7f4"
    elif title_text.startswith(("[中]", "[P2]", "P2", "中风险")):
        accent = "#c96442"
        background = "#fff8ee"
    else:
        accent = "#6f7f48"
        background = "#f4f7ee"
    return (
        '<li style="list-style-type:none;margin:14px 0 10px 0;padding:12px 14px;'
        f'border-left:4px solid {accent};border-radius:6px;background:{background};'
        'box-shadow:0 0 0 1px rgba(27,31,36,0.08);">'
        f'<strong style="display:block;font-size:15px;line-height:22px;color:#24292f;font-weight:700;">{title_html}</strong>'
        "</li>"
    )


def _inline_email_markdown_styles(rendered_html: str) -> str:
    replacements = {
        "<h1>": '<h1 style="font-size:22px;line-height:30px;margin:24px 0 12px 0;color:#24292f;font-weight:700;">',
        "<h2>": '<h2 style="font-size:18px;line-height:26px;margin:22px 0 10px 0;color:#24292f;font-weight:700;">',
        "<h3>": '<h3 style="font-size:16px;line-height:24px;margin:18px 0 8px 0;color:#24292f;font-weight:700;">',
        "<h4>": '<h4 style="font-size:15px;line-height:22px;margin:16px 0 8px 0;color:#24292f;font-weight:700;">',
        "<p>": '<p style="margin:0 0 12px 0;">',
        "<ul>": '<ul style="margin:0 0 14px 22px;padding:0;">',
        "<ol>": '<ol style="margin:0 0 14px 22px;padding:0;">',
        "<li>": '<li style="margin:0 0 6px 0;">',
        "<blockquote>": '<blockquote style="margin:0 0 14px 0;padding:0 0 0 14px;border-left:4px solid #d8dee4;color:#57606a;">',
        "<pre>": '<pre style="margin:0 0 14px 0;padding:12px;overflow-x:auto;background:#f6f8fa;border:1px solid #d8dee4;border-radius:6px;font-size:13px;line-height:20px;color:#24292f;">',
        "<code>": '<code style="font-family:SFMono-Regular,Consolas,Monaco,monospace;background:#f6f8fa;border-radius:4px;padding:1px 4px;font-size:13px;color:#24292f;">',
        "<table>": '<table style="border-collapse:collapse;width:100%;margin:0 0 14px 0;">',
        "<th>": '<th style="border:1px solid #d8dee4;padding:6px 8px;background:#f6f8fa;text-align:left;font-weight:700;">',
        "<td>": '<td style="border:1px solid #d8dee4;padding:6px 8px;vertical-align:top;">',
        "<hr>": '<hr style="border:0;border-top:1px solid #d8dee4;margin:18px 0;">',
    }
    styled = rendered_html
    for source, target in replacements.items():
        styled = styled.replace(source, target)
    styled = EMAIL_FINDING_TITLE_RE.sub(_style_email_finding_title, styled)
    styled = re.sub(r'<a href="([^"]+)">', r'<a href="\1" style="color:#0969da;text-decoration:none;">', styled)
    return styled
