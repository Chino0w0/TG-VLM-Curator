import { type FormEvent, useState } from "react";
import { type AdminApiClient, ApiClientError } from "../api/client";
import type { AdminSessionResponse } from "../api/types";

export interface LoginViewProps {
  client: AdminApiClient;
  onAuthenticated: (session: AdminSessionResponse) => void;
}

export function LoginView({ client, onAuthenticated }: LoginViewProps) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setErrorMessage(null);
    try {
      const session = await client.login(username.trim(), password);
      onAuthenticated(session);
    } catch (err) {
      if (err instanceof ApiClientError) {
        setErrorMessage(err.message);
      } else {
        setErrorMessage("Unable to reach administration API.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="login-container">
      <div className="login-card">
        <h1 style={{ marginBlockStart: 0, fontSize: "1.4rem" }}>
          TG-VLM-Curator Sign In
        </h1>
        <p className="workspace-subtitle" style={{ marginBlockEnd: "1.25rem" }}>
          Single-administrator Argon2id session authentication with HttpOnly
          cookie and CSRF protection.
        </p>

        {errorMessage ? (
          <div role="alert" className="alert-banner alert-error">
            {errorMessage}
          </div>
        ) : null}

        <form method="post" action="/api/v1/auth/login" onSubmit={handleSubmit}>
          <div className="field">
            <label htmlFor="admin-username">
              Username <span aria-hidden="true">*</span>
            </label>
            <input
              id="admin-username"
              name="username"
              type="text"
              autoComplete="username"
              autoCorrect="off"
              autoCapitalize="none"
              spellCheck={false}
              enterKeyHint="next"
              required
              value={username}
              onChange={(e) => {
                setUsername(e.target.value);
                if (errorMessage) setErrorMessage(null);
              }}
            />
          </div>

          <div className="field">
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
              }}
            >
              <label htmlFor="admin-current-password">
                Password <span aria-hidden="true">*</span>
              </label>
              <button
                type="button"
                className="btn"
                style={{ minBlockSize: "2rem", padding: "0.2rem 0.6rem", fontSize: "0.8rem" }}
                aria-pressed={showPassword}
                aria-describedby="password-reveal-warning"
                onClick={() => setShowPassword((prev) => !prev)}
              >
                {showPassword ? "Hide password" : "Show password"}
              </button>
            </div>
            <span id="password-reveal-warning" className="visually-hidden">
              Warning: toggling displays your administrator password on screen.
            </span>
            <input
              id="admin-current-password"
              name="password"
              type={showPassword ? "text" : "password"}
              autoComplete="current-password"
              enterKeyHint="done"
              required
              value={password}
              onChange={(e) => {
                setPassword(e.target.value);
                if (errorMessage) setErrorMessage(null);
              }}
            />
          </div>

          <button
            type="submit"
            className="btn btn-primary"
            style={{ inlineSize: "100%" }}
            disabled={submitting}
          >
            {submitting ? "Signing In..." : "Sign In"}
          </button>
        </form>
      </div>
    </main>
  );
}
