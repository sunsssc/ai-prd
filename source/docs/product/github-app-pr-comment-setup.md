# GitHub App 最小权限配置指南：让 Agent 在 PR 上写评论

## 范围

本文档只覆盖一个能力：**agent 通过 GitHub App 在指定仓库的 PR 上发布审查评论**。

不包含、也不申请：提交代码、创建 PR、修改 workflow、仓库管理、组织级权限。后续如需扩展见文末「后续扩展」。

---

## 一、需要申请的权限

创建 GitHub App 时，只勾选以下内容：

| 类别 | 权限 | 级别 | 用途 |
|------|------|------|------|
| Repository permissions | **Pull requests** | Read and write | read：拉取 PR 元信息、diff、变更文件列表；write：提交 review、发行内评论 |
| Repository permissions | Metadata | Read-only | 勾选 Pull requests 后自动带上，无需手动操作 |

其余一律不勾选：

- **Organization permissions：全部 None**
- **Account permissions：全部 None**
- Repository permissions 中的 Contents、Workflows、Actions、Checks、Issues、Administration 等：**全部不勾**

> 注：PR 的 diff 和变更文件通过 Pull requests 相关 API（`GET /pulls/{n}`、`GET /pulls/{n}/files`）获取，不需要 Contents 权限。
> 例外：如果 agent 需要用 App token `git clone/pull` 私有仓库做本地上下文分析（对应现有「代码仓库同步」链路），需额外加 **Contents: Read-only**——这是唯一可能需要的附加权限，且仍为只读。

Webhook：第一版延续现有设计的**轮询模式**，Webhook 保持 **Inactive**（不启用）。不启用 Webhook 就不需要公网回调地址，也不需要订阅任何事件。

---

## 二、配置步骤

1. **创建 App**：目标组织管理员进入 `Org Settings → Developer settings → GitHub Apps → New GitHub App`
   - Name：如 `<org>-pr-review-bot`（评论会以 `<name>[bot]` 身份显示）
   - Homepage URL：必填项，填内部文档地址即可
   - Webhook：**取消勾选 Active**
2. **配置权限**：按第一节勾选 `Pull requests: Read and write`
3. **生成私钥**：App 设置页底部 `Generate a private key`，下载 `.pem` 文件
   - 私钥存入密钥管理（部署环境的 secret），**禁止提交进仓库**
4. **记录 App ID**：App 设置页顶部（About 区块）的数字
5. **安装到组织**：`Install App → Install` → 选择目标组织 → 仓库范围选 **Only select repositories** → 只勾选需要自动 review 的仓库
   - 不要选 All repositories
6. **记录 Installation ID**：安装完成后浏览器 URL 形如 `/orgs/<org>/settings/installations/12345678`，末尾数字即 Installation ID

产出清单（agent 侧需要拿到的三样东西）：

```text
APP_ID=123456            # App 设置页
INSTALLATION_ID=12345678 # 安装 URL 末尾
private-key.pem          # 私钥文件（存密钥管理）
```

---

## 三、Agent 侧认证与调用

### 认证链

```text
App ID + 私钥 ──RS256 签名──▶ JWT（有效期 ≤10 分钟）
        │
        ▼ POST /app/installations/{INSTALLATION_ID}/access_tokens
Installation Access Token（有效期 1 小时，需缓存复用）
        │
        ▼ Authorization: Bearer <token>
调用 PR 相关 API
```

### 获取 Installation Token（Python 示例）

```python
import time
import jwt          # PyJWT
import requests

def get_installation_token(app_id: str, private_key_pem: str, installation_id: int) -> str:
    now = int(time.time())
    jwt_token = jwt.encode(
        {"iat": now - 60, "exp": now + 600, "iss": app_id},  # exp 最长 10 分钟，iat 回拨 60s 防时钟偏差
        private_key_pem,
        algorithm="RS256",
    )
    resp = requests.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["token"]  # 响应含 expires_at，应缓存到过期前约 5 分钟再重新获取
```

### 发布审查评论（推荐：整份 review 一次提交）

```python
def submit_review(owner, repo, pr_number, head_sha, summary, inline_comments, token):
    resp = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        json={
            "commit_id": head_sha,       # PR 最新 head commit sha
            "event": "COMMENT",          # 仅评论：不 approve、不 request changes、不阻塞合并
            "body": summary,             # review 总体说明
            "comments": [
                {
                    "path": c["path"],   # 文件路径
                    "line": c["line"],   # diff 右侧（新文件）行号；评论被删代码时用 side="LEFT"
                    "body": c["body"],
                }
                for c in inline_comments
            ],
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["html_url"]
```

要点：

- `event` 固定用 `COMMENT`，符合现有设计「不 approve、不 request changes、不阻塞合并」的非目标约束
- 行内评论的 `line` 是 diff 新文件侧的行号；跨行评论追加 `start_line`
- 若只需发单条评论，可用 `POST /repos/{owner}/{repo}/pulls/{n}/comments`，参数同为 `body/commit_id/path/line`
- 评论以 `<app-name>[bot]` 机器人身份显示，不占用组织用户席位

### 拉取 PR 数据（配合现有轮询链路）

```text
GET /repos/{owner}/{repo}/pulls?state=open           # Pull requests: read
GET /repos/{owner}/{repo}/pulls/{n}/files            # 变更文件 + patch 片段
GET /repos/{owner}/{repo}/pulls/{n}                  # 元信息，Accept: application/vnd.github.diff 可直接拿完整 diff
```

---

## 四、验收方式

1. 在任一已配置仓库手工开一个测试 PR
2. agent 用上述流程对该 PR 提交一条 review（含至少一条行内评论）
3. PR 页面确认：出现 `<app-name>[bot]` 身份的 review，行内评论挂在正确文件行上
4. 反向验证最小权限：用同一 token 尝试写文件内容（`PUT /repos/{owner}/{repo}/contents/x`），应返回 403

---

## 五、最小权限自查清单

- [ ] Repository permissions 只勾了 Pull requests: Read and write（如有本地克隆需求，额外 Contents: Read-only）
- [ ] Organization permissions / Account permissions 均为 None
- [ ] Webhook 未启用（轮询模式不需要）
- [ ] 安装范围为 Only select repositories，仅含目标 review 仓库
- [ ] 私钥存密钥管理，未进仓库、未进日志
- [ ] Installation token 缓存复用（1 小时有效期），不逐请求申请

---

## 六、后续扩展（本期不实施）

| 需求 | 追加项 |
|------|--------|
| 事件驱动替代轮询 | 启用 Webhook（公网 HTTPS 回调 + secret 验签），订阅 `pull_request` 事件（`review_requested` 是其 action） |
| 自动提交代码 / 建 PR | 加 Contents: Read and write（建 PR 本身 Pull requests 权限已覆盖） |
| 展示 CI 式审查状态 | 加 Checks: Read and write |

扩展时回到本文档更新权限清单与自查项，避免权限静默膨胀。
