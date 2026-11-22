//! Creating and upgrading machines: everything that runs on the sandbox goes through
//! `provision.sh`, which is idempotent.

use crate::boat::Boat;
use crate::proxy::auth::random_token;
use crate::store::Machine;
use anyhow::{anyhow, bail, Context, Result};
use base64::Engine;
use serde::Deserialize;

const PROVISION_SH: &str = include_str!("provision.sh");
const WATCHDOG_MJS: &str = include_str!("watchdog.mjs");
const MCP_HTTP_BRIDGE_PY: &str = include_str!("mcp_http_bridge.py");
const CONFIGURE_ANTIGRAVITY_MCP_PY: &str = include_str!("configure_antigravity_mcp.py");
const T3_PORT: u16 = 3773;
const WATCHDOG_PORT: u16 = 3774;

/// Identifies the provisioning scripts; a machine provisioned with others gets them on wake.
pub fn revision() -> String {
    let hash = fnv1a(
        PROVISION_SH
            .bytes()
            .chain(WATCHDOG_MJS.bytes())
            .chain(MCP_HTTP_BRIDGE_PY.bytes())
            .chain(CONFIGURE_ANTIGRAVITY_MCP_PY.bytes())
            .chain(SLEEP_AFTER_SECS.to_string().bytes()),
    );
    format!("{hash:016x}")
}

/// FNV-1a: stable across builds, unlike std's hasher.
pub fn fnv1a(bytes: impl IntoIterator<Item = u8>) -> u64 {
    bytes.into_iter().fold(0xcbf29ce484222325, |h, b| (h ^ u64::from(b)).wrapping_mul(0x100000001b3))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Provisioned {
    pub environment_id: String,
    pub upstream_token: Option<String>,
}

/// Machine sizes, as Boat sells them.
#[derive(Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SizeInfo {
    pub id: &'static str,
    pub spec: &'static str,
    pub hourly: f64,
}

pub const SIZES: &[SizeInfo] = &[
    SizeInfo { id: "small", spec: "2 vCPU · 4 GB", hourly: 0.018 },
    SizeInfo { id: "default", spec: "4 vCPU · 8 GB", hourly: 0.036 },
    SizeInfo { id: "large", spec: "8 vCPU · 16 GB", hourly: 0.072 },
];

/// Curated Office desk presets combining Boat environment, snapshot, and size.
#[derive(Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DeskPreset {
    pub id: &'static str,
    pub label: &'static str,
    pub default_name: &'static str,
    pub size: &'static str,
    pub environment: Option<&'static str>,
    pub snapshot: Option<&'static str>,
    pub summary: &'static str,
}

pub const PRESETS: &[DeskPreset] = &[
    DeskPreset {
        id: "poppy",
        label: "Poppy Desk",
        default_name: "poppy-desk",
        size: "large",
        environment: Some("poppy"),
        snapshot: Some("t3-poppy-v3"),
        summary: "Poppy env · video/QA + Antigravity",
    },
    DeskPreset {
        id: "territory",
        label: "Territory Desk",
        default_name: "territory-desk",
        size: "large",
        environment: Some("territory"),
        snapshot: Some("t3-territory-v2"),
        summary: "Territory env · brand-walled · video/QA",
    },
    DeskPreset {
        id: "babyak-lab",
        label: "Lab Desk",
        default_name: "lab-desk",
        size: "default",
        environment: Some("t3-babyak-lab"),
        snapshot: Some("t3-babyak-lab-v2"),
        summary: "Babyak Lab overseer env · Antigravity",
    },
    DeskPreset {
        id: "custom",
        label: "Base / Custom",
        default_name: "",
        size: "default",
        environment: None,
        snapshot: None,
        summary: "Default Boat env · clean sandbox",
    },
];

/// Maps a Boat environment name to the Office MCP/brand-wall scope passed to `provision.sh`.
pub fn office_env_for(environment: Option<&str>) -> &'static str {
    match environment.unwrap_or("base") {
        "poppy" | "poppy-performance" => "poppy",
        "territory" | "t3-territory" => "territory",
        "t3-babyak-lab" | "babyak-lab" => "babyak-lab",
        _ => "base",
    }
}

pub fn size(id: &str) -> Result<&'static SizeInfo> {
    SIZES.iter().find(|s| s.id == id).ok_or_else(|| anyhow!("unknown size {id}"))
}

/// How long a machine stays up after the last agent turn and the last action through t3boat.
/// Long enough to read a result and type a follow-up without waiting for a wake; idle minutes
/// cost under a cent even on the large size.
pub const SLEEP_AFTER_SECS: u64 = 300;

/// The machine's sandbox, as Boat lists it.
pub fn sandbox_name(name: &str) -> String {
    format!("t3boat · {name}")
}

/// The saved snapshot the watchdog replaces at every sleep. Boat deletes a stopped sandbox's
/// disk after 14 days; this stays, so the machine can be restored from it. Unique per machine,
/// whoever else uses the Boat account. Boat takes lowercase letters, digits and dashes.
pub fn snapshot_name(name: &str, machine_id: &str) -> String {
    let id: String = machine_id.chars().map(|c| if c.is_ascii_alphanumeric() { c.to_ascii_lowercase() } else { '-' }).collect();
    format!("t3boat-{name}-{id}")
}

/// Copies the scripts over as a file (the watchdog secret must not appear in any command line)
/// and runs `provision.sh`.
pub async fn run(
    boat: &Boat,
    sandbox_id: &str,
    name: &str,
    t3_version: &str,
    watchdog_secret: &str,
    snapshot: &str,
    office_env: &str,
    mint_upstream_token: bool,
) -> Result<Provisioned> {
    let b64 = |s: &str| base64::engine::general_purpose::STANDARD.encode(s);
    let script = format!(
        "set -e; d=$(mktemp -d); trap 'rm -rf \"$d\" \"$0\"' EXIT\n\
         echo {p} | base64 -d > \"$d/provision.sh\"\n\
         echo {w} | base64 -d > \"$d/watchdog.mjs\"\n\
         echo {b} | base64 -d > \"$d/mcp_http_bridge.py\"\n\
         echo {c} | base64 -d > \"$d/configure_antigravity_mcp.py\"\n\
         T3BOAT_SECRET={s} bash \"$d/provision.sh\" {v} {sleep} {snapshot} {mint} {name} {env}\n",
        p = b64(PROVISION_SH),
        w = b64(WATCHDOG_MJS),
        b = b64(MCP_HTTP_BRIDGE_PY),
        c = b64(CONFIGURE_ANTIGRAVITY_MCP_PY),
        v = shell_word(t3_version)?,
        s = shell_word(watchdog_secret)?,
        sleep = SLEEP_AFTER_SECS,
        snapshot = shell_word(snapshot)?,
        mint = u8::from(mint_upstream_token),
        name = shell_word(name)?,
        env = shell_word(office_env)?,
    );
    let name = format!("t3boat-provision-{}.sh", random_token());
    let local = std::env::temp_dir().join(&name);
    crate::store::write_private(&local, script.as_bytes())?;
    let remote = format!("/tmp/{name}");
    let uploaded = boat.upload(sandbox_id, &local, &remote).await;
    let _ = std::fs::remove_file(&local);
    uploaded?;
    let out = boat.exec_ok(sandbox_id, &format!("bash {remote}"), 600).await?;
    let line = out
        .lines()
        .rev()
        .find(|l| l.starts_with('{'))
        .ok_or_else(|| anyhow!("provisioning printed no result"))?;
    Ok(serde_json::from_str(line)?)
}

/// Only `[A-Za-z0-9._-]` reaches the remote shell.
pub(crate) fn shell_word(s: &str) -> Result<&str> {
    if s.is_empty() || !s.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-')) {
        bail!("unsafe value for the remote shell: {s}");
    }
    Ok(s)
}

/// Hosts the T3 server's and the watchdog's ports publicly: their URLs, in that order.
/// Idempotent: hosting again keeps the URLs.
pub async fn host(boat: &Boat, sandbox_id: &str) -> Result<(String, String)> {
    tokio::try_join!(boat.host_public(sandbox_id, T3_PORT), boat.host_public(sandbox_id, WATCHDOG_PORT))
}

/// Creates a new machine end to end. Takes about a minute, most of it Boat and the T3 install.
/// On failure after `created`, the caller deletes the sandbox.
#[allow(clippy::too_many_arguments)]
pub async fn create(
    boat: &Boat,
    name: &str,
    size: &str,
    environment: Option<&str>,
    base_snapshot: Option<&str>,
    t3_version: &str,
    local_port: u16,
    created: impl FnOnce(&str),
    progress: impl Fn(&str),
) -> Result<Machine> {
    self::size(size)?;
    let environment = environment.map(str::trim).filter(|s| !s.is_empty());
    let base_snapshot = base_snapshot.map(str::trim).filter(|s| !s.is_empty());
    if let Some(env) = environment {
        shell_word(env)?;
    }
    if let Some(snap) = base_snapshot {
        shell_word(snap)?;
    }
    progress("Creating sandbox on Boat");
    let id = boat
        .create(&sandbox_name(name), size, environment, base_snapshot)
        .await
        .context("creating the Boat sandbox")?;
    created(&id);
    progress("Installing T3 Code & Antigravity");
    let secret = random_token();
    let office_env = office_env_for(environment);
    let provisioned = run(
        boat,
        &id,
        name,
        t3_version,
        &secret,
        &snapshot_name(name, &id),
        office_env,
        true,
    )
    .await?;
    progress("Opening the machine's address");
    let (upstream_url, watchdog_url) = host(boat, &id).await?;
    Ok(Machine {
        id: id.clone(),
        sandbox_id: id,
        name: name.to_owned(),
        size: size.to_owned(),
        environment: environment.map(str::to_owned),
        base_snapshot: base_snapshot.map(str::to_owned),
        upstream_url,
        upstream_token: provisioned
            .upstream_token
            .ok_or_else(|| anyhow!("provisioning issued no token"))?,
        watchdog_url,
        watchdog_secret: secret,
        local_port,
        t3_version: t3_version.to_owned(),
        provision_revision: revision(),
        environment_id: provisioned.environment_id,
        client_tokens: Vec::new(),
        link_tokens: Vec::new(),
        created_at: chrono::Utc::now().timestamp(),
        next_due_at: None,
        connect: false,
    })
}

#[cfg(test)]
mod tests {
    use super::{office_env_for, shell_word};

    #[test]
    fn maps_boat_environments_to_office_scopes() {
        assert_eq!(office_env_for(Some("poppy")), "poppy");
        assert_eq!(office_env_for(Some("poppy-performance")), "poppy");
        assert_eq!(office_env_for(Some("territory")), "territory");
        assert_eq!(office_env_for(Some("t3-territory")), "territory");
        assert_eq!(office_env_for(Some("t3-babyak-lab")), "babyak-lab");
        assert_eq!(office_env_for(Some("base")), "base");
        assert_eq!(office_env_for(None), "base");
    }

    #[test]
    fn shell_word_rejects_empty_and_metacharacters() {
        assert!(shell_word("t3-poppy-v3").is_ok());
        assert!(shell_word("t3_babyak.lab").is_ok());
        assert!(shell_word("").is_err());
        assert!(shell_word("poppy; rm -rf /").is_err());
        assert!(shell_word("poppy$(id)").is_err());
    }
}
