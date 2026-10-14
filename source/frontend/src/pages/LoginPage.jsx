import { useEffect, useState } from "react";
import BrandLogo from "../components/branding/BrandLogo";
import Card from "../components/common/Card";
import {
  getAuthConfig,
  getGoogleAuthorizationUrl,
  loginWithEmail,
  registerWithEmail,
  requestEmailCode,
  revealEmailCode
} from "../services/authApi";

export default function LoginPage({ onAuthenticated, authError = "" }) {
  const isDev = import.meta.env.DEV;
  const [emailAuthEnabled, setEmailAuthEnabled] = useState(false);
  const [emailPurpose, setEmailPurpose] = useState("");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState(authError);
  const [googleSubmitting, setGoogleSubmitting] = useState(false);
  const [requestingCode, setRequestingCode] = useState(false);
  const [revealingCode, setRevealingCode] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    setError(authError);
  }, [authError]);

  useEffect(() => {
    let active = true;
    getAuthConfig()
      .then((payload) => {
        if (active) {
          setEmailAuthEnabled(Boolean(payload?.email_auth_enabled));
        }
      })
      .catch(() => {
        if (active) {
          setEmailAuthEnabled(false);
        }
      });
    return () => {
      active = false;
    };
  }, []);

  async function handleGoogleLogin() {
    setError("");
    setGoogleSubmitting(true);
    try {
      const authorizationUrl = await getGoogleAuthorizationUrl();
      window.location.assign(authorizationUrl);
    } catch (loginError) {
      setGoogleSubmitting(false);
      setError(loginError.message || "Google 登录地址获取失败，请稍后重试。");
    }
  }

  async function handleRequestCode() {
    if (!email.trim()) {
      setError("请先输入邮箱。");
      return;
    }

    setRequestingCode(true);
    setEmailPurpose("");
    setError("");
    setMessage("");
    try {
      const payload = await requestEmailCode(email.trim(), "auto");
      if (!payload.purpose) {
        throw new Error("验证码已发送，但未能确认认证方式。");
      }
      setEmailPurpose(payload.purpose);
      setMessage(payload.reason || "验证码已发送，请检查邮箱。");
      if (payload.debug_code) {
        setCode(payload.debug_code);
      }
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setRequestingCode(false);
    }
  }

  async function handleRevealCode() {
    if (!email.trim() || !emailPurpose) {
      setError("请先输入邮箱并发送验证码。");
      return;
    }

    setRevealingCode(true);
    setError("");
    try {
      const payload = await revealEmailCode(email.trim(), emailPurpose);
      if (!payload.code) {
        throw new Error("当前没有可用的本地验证码。");
      }
      setCode(payload.code);
      setMessage("已填入本地验证码。");
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setRevealingCode(false);
    }
  }

  async function handleEmailSubmit(event) {
    event.preventDefault();
    if (!email.trim() || !code.trim()) {
      setError("请先填写邮箱和验证码。");
      return;
    }
    if (!emailPurpose) {
      setError("请先发送验证码。");
      return;
    }

    setSubmitting(true);
    setError("");
    setMessage("");
    try {
      const payload = emailPurpose === "login"
        ? await loginWithEmail(email.trim(), code.trim())
        : await registerWithEmail(email.trim(), null, code.trim());
      if (!payload?.user) {
        throw new Error("认证成功，但未获取到用户信息。");
      }
      onAuthenticated?.(payload.user);
    } catch (submitError) {
      setError(submitError.message);
    } finally {
      setSubmitting(false);
    }
  }

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const authErrorMessage = params.get("auth_error");
    if (!authErrorMessage) {
      return;
    }
    setError(authErrorMessage);
    params.delete("auth_error");
    const nextSearch = params.toString();
    window.history.replaceState(null, "", `${window.location.pathname}${nextSearch ? `?${nextSearch}` : ""}${window.location.hash}`);
  }, []);

  return (
    <div className="auth-shell">
      <div className="auth-hero">
        <div className="auth-brand-lockup">
          <BrandLogo className="auth-brand-logo" />
          <p className="auth-eyebrow">AI Product Workspace</p>
        </div>
        <h1 className="auth-title">进入 ai-prd</h1>
        <p className="auth-subtitle">
          {emailAuthEnabled
            ? "企业账号可使用 Google 登录；当前环境也支持已加入名单的邮箱验证码认证。"
            : "使用 Google 账号注册或登录。注册成功后即可使用通用功能，Agent 需另行向管理员申请。"}
        </p>
      </div>

      <Card className="auth-card">
        <button
          type="button"
          className="button button-secondary auth-google-button"
          onClick={handleGoogleLogin}
          disabled={googleSubmitting}
        >
          <span className="auth-google-mark" aria-hidden="true">
            G
          </span>
          {googleSubmitting ? "Google 登录中..." : "使用 Google 注册 / 登录"}
        </button>

        {error ? <p className="auth-error" role="alert">{error}</p> : null}

        {emailAuthEnabled ? (
          <>
            <div className="auth-divider" aria-hidden="true">
              <span />
              <span>或使用邮箱</span>
              <span />
            </div>

            <form className="auth-form" onSubmit={handleEmailSubmit}>
              <label className="auth-field">
                <span className="auth-label">邮箱</span>
                <input
                  className="auth-input"
                  type="email"
                  autoComplete="email"
                  value={email}
                  placeholder="输入已加入名单的邮箱"
                  onChange={(event) => setEmail(event.target.value)}
                />
              </label>

              <div className="auth-code-row">
                <label className="auth-field auth-field-code">
                  <span className="auth-label">验证码</span>
                  <input
                    className="auth-input"
                    type="text"
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    value={code}
                    placeholder="输入邮箱验证码"
                    onChange={(event) => setCode(event.target.value)}
                  />
                </label>
                <button
                  type="button"
                  className="button button-secondary auth-code-button"
                  onClick={handleRequestCode}
                  disabled={requestingCode}
                >
                  {requestingCode ? "发送中..." : "发送验证码"}
                </button>
              </div>

              {isDev && emailPurpose ? (
                <button
                  type="button"
                  className="auth-reveal-button"
                  onClick={handleRevealCode}
                  disabled={revealingCode}
                >
                  {revealingCode ? "读取中..." : "读取本地验证码"}
                </button>
              ) : null}

              {message ? <p className="auth-hint" role="status">{message}</p> : null}

              <button type="submit" className="button button-send auth-submit-button" disabled={submitting}>
                {submitting ? "处理中..." : "使用邮箱继续"}
              </button>
            </form>
          </>
        ) : null}
      </Card>
    </div>
  );
}
