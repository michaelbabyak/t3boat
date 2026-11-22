//! Signing an agent in on a machine with the vendor's own CLI login. The login runs on the
//! machine and its localhost callback port is forwarded to this computer, so the browser's
//! redirect after "Authorize" lands on the machine: no code to paste, and the credential is
//! created on the machine and stays there. This computer's own logins are never touched.

use super::MachineRuntime;
use crate::proxy::upstream;
use anyhow::{bail, Context, Result};
use std::time::{Duration, Instant};

const URL_TIMEOUT: Duration = Duration::from_secs(30);
const LOGIN_TIMEOUT: Duration = Duration::from_secs(600);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Agent {
    Claude,
    Codex,
    Antigravity,
}

impl Agent {
    /// By the provider's display name, as `signedOut` lists it.
    pub fn from_name(name: &str) -> Option<Self> {
        match name {
            "Claude" => Some(Self::Claude),
            "Codex" => Some(Self::Codex),
            "Antigravity" => Some(Self::Antigravity),
            _ => None,
        }
    }

    fn login(self) -> &'static str {
        match self {
            Self::Claude => "claude auth login",
            Self::Codex => "codex login",
            // Enforce Google OAuth subscription only (never API keys).
            Self::Antigravity => "env -u GEMINI_API_KEY -u GOOGLE_API_KEY agy -p \"Reply ONLY: OK\"",
        }
    }

    /// Exits 0 once signed in.
    fn status(self) -> &'static str {
        match self {
            Self::Claude => "claude auth status >/dev/null 2>&1",
            Self::Codex => "codex login status >/dev/null 2>&1",
            Self::Antigravity => "env -u GEMINI_API_KEY -u GOOGLE_API_KEY agy models >/dev/null 2>&1",
        }
    }

    /// Claude and Codex bind a localhost callback port that must be forwarded back to this Mac.
    /// Antigravity uses Google's hosted callback (`https://antigravity.google/oauth-callback`)
    /// and polls for completion, so no localhost port forward is required.
    fn needs_local_callback(self) -> bool {
        !matches!(self, Self::Antigravity)
    }
}

impl MachineRuntime {
    pub async fn sign_in(&self, agent: Agent) -> Result<()> {
        self.ensure_awake().await?;
        let id = self.sandbox_id();
        let boat = &self.shared.boat;
        // The CLI hands the sign-in URL to $BROWSER (or prints it to stdout in headless mode).
        // Record the exact spawned PID so cleanup only terminates this sign-in attempt.
        let dir = boat
            .exec_ok(
                &id,
                &format!(
                    "export PATH=\"$HOME/.local/bin:/usr/local/bin:$PATH\"; \
                     d=$(mktemp -d); printf '#!/bin/sh\\necho \"$1\" > %s/url\\n' \"$d\" > \"$d/browser\"; \
                     chmod +x \"$d/browser\"; \
                     BROWSER=\"$d/browser\" nohup script -qfec '{}' \"$d/log\" >/dev/null 2>&1 & echo $! > \"$d/pid\"; echo \"$d\"",
                    agent.login()
                ),
                30,
            )
            .await?;
        let dir = dir.lines().last().unwrap_or_default().trim().to_owned();
        // The machine must not fall asleep while the person is in the browser.
        let result = self.kept_awake(self.finish_sign_in(agent, &dir)).await;
        let _ = boat
            .exec(
                &id,
                &format!(
                    "if [ -f '{dir}/pid' ]; then pid=$(cat '{dir}/pid'); pkill -P \"$pid\" 2>/dev/null || true; kill \"$pid\" 2>/dev/null || true; fi; rm -rf '{dir}'"
                ),
                30,
            )
            .await;
        result?;
        // T3 re-checks the provider and tells T3 Code; the sign-in nudge goes away with it, and
        // the recorded config learns it too.
        let refreshed = upstream::call(
            &self.upstream_url(),
            &self.upstream_token(),
            "server.refreshProviders",
            serde_json::json!({}),
        )
        .await;
        if let Ok(result) = refreshed {
            self.cache.lock().unwrap().fold_providers(&result["providers"]);
            self.save_cache();
        }
        Ok(())
    }

    async fn finish_sign_in(&self, agent: Agent, dir: &str) -> Result<()> {
        let id = self.sandbox_id();
        let boat = &self.shared.boat;
        let started = Instant::now();
        let url = loop {
            let out = boat
                .exec(&id, &format!("cat '{dir}/url' '{dir}/log' 2>/dev/null"), 30)
                .await?;
            if let Some(url) = extract_https_url(&out.stdout) {
                break url;
            }
            if started.elapsed() > URL_TIMEOUT {
                bail!("the {agent:?} login did not start on the machine");
            }
            tokio::time::sleep(Duration::from_millis(300)).await;
        };
        let _forward = match callback_port(&url) {
            Some(port) => Some(
                boat.forward(&id, port)
                    .await
                    .with_context(|| format!("forwarding port {port} for the {agent:?} login"))?,
            ),
            None if agent.needs_local_callback() => {
                bail!("the {agent:?} login has no local callback");
            }
            None => None,
        };
        crate::app::open_url(&url)?;
        loop {
            tokio::time::sleep(Duration::from_secs(3)).await;
            if boat
                .exec(
                    &id,
                    &format!("export PATH=\"$HOME/.local/bin:/usr/local/bin:$PATH\"; {}", agent.status()),
                    30,
                )
                .await?
                .exit_code
                == Some(0)
            {
                return Ok(());
            }
            if started.elapsed() > LOGIN_TIMEOUT {
                bail!("the {agent:?} sign-in was not finished in the browser");
            }
        }
    }
}

/// Extracts the first `https://...` URL from `$BROWSER` output or PTY log, trimming ANSI/control noise.
fn extract_https_url(text: &str) -> Option<String> {
    for token in text.split_whitespace() {
        let Some(start) = token.find("https://") else { continue };
        let clean: String = token[start..]
            .chars()
            .take_while(|c| !c.is_control() && !matches!(c, '"' | '\'' | '<' | '>' | ')'))
            .collect();
        if url::Url::parse(&clean).is_ok() {
            return Some(clean);
        }
    }
    None
}

/// The port of a `redirect_uri=http://localhost:<port>/...` (or 127.0.0.1) parameter.
fn callback_port(url: &str) -> Option<u16> {
    let url = url::Url::parse(url).ok()?;
    let redirect = url.query_pairs().find(|(k, _)| k == "redirect_uri")?.1;
    let redirect = url::Url::parse(&redirect).ok()?;
    matches!(redirect.host_str()?, "localhost" | "127.0.0.1").then_some(())?;
    redirect.port()
}

#[cfg(test)]
mod tests {
    use super::{callback_port, extract_https_url, Agent};

    #[test]
    fn finds_the_local_callback_port() {
        let claude = "https://claude.com/cai/oauth/authorize?code=true&redirect_uri=http%3A%2F%2Flocalhost%3A43327%2Fcallback&state=x";
        let codex = "https://auth.openai.com/oauth/authorize?redirect_uri=http%3A%2F%2F127.0.0.1%3A1455%2Fauth%2Fcallback";
        let manual = "https://claude.com/cai/oauth/authorize?redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback";
        let agy = "https://accounts.google.com/o/oauth2/auth?redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback&state=abc";
        assert_eq!(callback_port(claude), Some(43327));
        assert_eq!(callback_port(codex), Some(1455));
        assert_eq!(callback_port(manual), None);
        assert_eq!(callback_port(agy), None);
        assert_eq!(Agent::from_name("Antigravity"), Some(Agent::Antigravity));
        assert!(!Agent::Antigravity.needs_local_callback());
        assert!(Agent::Claude.needs_local_callback());
        assert_eq!(
            extract_https_url("Authentication required.\r\n  https://accounts.google.com/o/oauth2/auth?state=1\x1b[0m\r\n"),
            Some("https://accounts.google.com/o/oauth2/auth?state=1".to_owned())
        );
    }
}
