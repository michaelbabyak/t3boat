#!/usr/bin/env python3
"""Configure Antigravity MCP servers (`mcp_config.json`) cleanly for local Mac or cloud Boat boxes.

Solves two concrete parity bugs without ever writing or printing a secret:
  1. Offices MCP: Antigravity ignores Claude Code's `headersHelper` key. Replaces it with
     stdio `mcp_http_bridge.py` (`--headers-helper` on local Mac; `--bearer-env-or-file` +
     `--header-env-default` on cloud boxes).
  2. Composio MCP: Points at `https://connect.composio.dev/mcp` with header
     `x-consumer-api-key` read at runtime from `COMPOSIO_CONSUMER_API_KEY` (fallback
     `COMPOSIO_API_KEY`) via `mcp_http_bridge.py`, replacing legacy `backend.composio.dev`
     or broken `${COMPOSIO_API_KEY}` entries.
  3. Brand-wall enforcement (cloud mode):
     - Non-Poppy cloud scopes strip any Poppy DW / Postgres MCP entries by name or content.
     - Non-Territory cloud scopes strip any Territory Ryze / Mealogic MCP entries by name or content.
     - Embedded Postgres URIs and env-var secret values are refused across ALL cloud scopes.
     - Cloud mode does not write `.bak` copies of stripped/legacy configs on disk.

Targets every Antigravity config file present under `HOME`:
  - `~/.gemini/config/mcp_config.json` (created if missing)
  - `~/.gemini/antigravity/mcp_config.json` (updated if present)
  - `~/.t3/userdata/providers/antigravity/*/config/mcp_config.json` (updated if present)

In local mode, creates a 0600 `.bak.<utc>` backup before modifying an existing file
(cloud mode omits backups so stripped/cross-tenant configs are never retained on disk).
Stdlib only.
"""
import argparse
from datetime import datetime, timezone
import glob
import json
import os
from pathlib import Path
import secrets
import sys

OFFICES_MCP_URL = "https://offices.babyak-botster.workers.dev/mcp"
COMPOSIO_MCP_URL = "https://connect.composio.dev/mcp"
COMPOSIO_HEADER = "x-consumer-api-key"
COMPOSIO_VARS_DEFAULT = "COMPOSIO_CONSUMER_API_KEY,COMPOSIO_API_KEY"
COMPOSIO_VARS_BABYAK_LAB = "COMPOSIO_CONSUMER_API_KEY,COMPOSIO_API_KEY_BABYAK_LAB,COMPOSIO_API_KEY"
RYZE_MCP_URL = "https://connector.get-ryze.ai/mcp"
OFFICES_BEARER_SPEC = (
    "OFFICES_CALLER_KEY,OFFICES_READ_KEY:"
    "/home/user/.offices-job-key,/dev/shm/offices-t3/offices.key,/home/user/.config/offices/.offices-read-key"
)
OFFICES_CALLER_DEFAULT = "x-offices-caller=OFFICES_CALLER:generalist:t3"


def discover_targets(home):
    home = Path(home)
    targets = [home / ".gemini" / "config" / "mcp_config.json"]
    legacy = home / ".gemini" / "antigravity" / "mcp_config.json"
    if legacy.exists():
        targets.append(legacy)
    pattern = str(home / ".t3" / "userdata" / "providers" / "antigravity" / "*" / "config" / "mcp_config.json")
    for p in sorted(glob.glob(pattern)):
        targets.append(Path(p))
    # Deduplicate preserving order
    seen, out = set(), []
    for t in targets:
        key = str(t)
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def offices_entry(mode, bridge_path, helper_path=None):
    bridge_path = str(bridge_path)
    if mode == "local":
        if not helper_path:
            raise ValueError("local mode requires --headers-helper")
        return {
            "command": "python3",
            "args": [
                bridge_path,
                "--url", OFFICES_MCP_URL,
                "--headers-helper", str(helper_path),
            ],
        }
    return {
        "command": "python3",
        "args": [
            bridge_path,
            "--url", OFFICES_MCP_URL,
            "--bearer-env-or-file", OFFICES_BEARER_SPEC,
            "--header-env-default", OFFICES_CALLER_DEFAULT,
        ],
    }


def composio_bridge_entry(bridge_path, office_env="babyak-lab"):
    env_vars = COMPOSIO_VARS_BABYAK_LAB if office_env == "babyak-lab" else COMPOSIO_VARS_DEFAULT
    return {
        "command": "python3",
        "args": [
            str(bridge_path),
            "--url", COMPOSIO_MCP_URL,
            "--header-env", f"{COMPOSIO_HEADER}={env_vars}",
        ],
    }


def ryze_bridge_entry(bridge_path):
    return {
        "command": "python3",
        "args": [
            str(bridge_path),
            "--url", RYZE_MCP_URL,
            "--bearer-env-or-file", "RYZE_API_KEY:",
        ],
    }


def _looks_broken_composio(entry):
    if not isinstance(entry, dict):
        return True
    url = str(entry.get("url") or "")
    if "backend.composio.dev" in url:
        return True
    headers = entry.get("headers") or {}
    if isinstance(headers, dict):
        for k, v in headers.items():
            if k.lower() == "x-api-key" or "${" in str(v):
                return True
    return False


def _read_json(path):
    if path.exists() and path.read_text(encoding="utf-8").strip():
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path} is not a JSON object")
        return data
    return {}


def _iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield k
            yield from _iter_strings(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_strings(item)


def _write_atomic(path, text, backup=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if backup and path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = path.with_name(f"{path.name}.bak.{stamp}")
        if not backup_path.exists():
            # Create the backup with 0600 BEFORE writing any bytes (never expose readable source perms).
            raw = path.read_bytes()
            fd = os.open(str(backup_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as bf:
                    bf.write(raw)
            except Exception:
                os.unlink(backup_path)
                raise
    tmp = path.with_name(f"{path.name}.offices-tmp.{os.getpid()}.{secrets.token_hex(6)}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tf:
            tf.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return backup_path


def apply_config(home, mode, bridge_path, office_env="babyak-lab", helper_path=None,
                 force_composio=False, dry_run=False, environ=None):
    environ = dict(os.environ) if environ is None else dict(environ)
    home = Path(home)
    targets = discover_targets(home)
    if mode == "local" and helper_path is None:
        helper_path = home / ".config" / "offices" / "headers.sh"

    results = []
    # Collect known secret values (from env) purely to assert NONE are ever written to disk.
    secret_values = [
        v for k, v in environ.items()
        if v and len(v) >= 8 and any(tok in k.upper() for tok in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    ]

    for target in targets:
        data = _read_json(target)
        servers = data.get("mcpServers")
        if servers is None:
            servers = data["mcpServers"] = {}
        if not isinstance(servers, dict):
            raise ValueError(f"{target}: mcpServers is not an object")

        changes = []

        # 1. Offices MCP — always replace broken headersHelper / configure stdio bridge.
        new_offices = offices_entry(mode, bridge_path, helper_path=helper_path)
        if servers.get("offices") != new_offices:
            servers["offices"] = new_offices
            changes.append("offices:bridge")

        # 2. Composio MCP
        existing_composio = servers.get("composio")
        if mode == "cloud" or force_composio or existing_composio is None or _looks_broken_composio(existing_composio):
            # In cloud mode always use the secret-free stdio bridge; in local mode preserve an
            # already-working connect.composio.dev consumer key entry unless --force-composio.
            if mode == "cloud" or force_composio or _looks_broken_composio(existing_composio):
                new_composio = composio_bridge_entry(bridge_path, office_env=office_env)
                if servers.get("composio") != new_composio:
                    servers["composio"] = new_composio
                    changes.append("composio:bridge")

        # 2b. Territory-only MCP servers (secret-free stdio/bridge; strictly excluded from Poppy/Lab).
        if mode == "cloud" and office_env == "territory":
            new_ryze = ryze_bridge_entry(bridge_path)
            if servers.get("ryze") != new_ryze:
                servers["ryze"] = new_ryze
                changes.append("ryze:bridge")
            mealogic_script = home / ".config" / "offices" / "mealogic_mcp.py"
            if mealogic_script.exists():
                new_mealogic = {"command": "python3", "args": [str(mealogic_script)]}
                if servers.get("mealogic-local") != new_mealogic:
                    servers["mealogic-local"] = new_mealogic
                    changes.append("mealogic-local:stdio")

        # 3. Brand-wall checks (cloud mode):
        #    - Non-Poppy cloud scopes strip Poppy DW / Postgres servers by name OR content.
        #    - Non-Territory cloud scopes strip Territory Ryze / Mealogic servers by name OR content.
        if mode == "cloud":
            forbidden_markers = []
            if office_env not in ("poppy", "poppy-performance"):
                forbidden_markers.extend((
                    "datawarehouse", "data-warehouse", "data_warehouse",
                    "poppy-dw", "poppy_dw", "poppy-data-warehouse", "poppy_data_warehouse",
                    "postgres",
                ))
            if office_env != "territory":
                forbidden_markers.extend((
                    "ryze", "get-ryze.ai", "mealogic", "mealogic-mcp",
                ))
            if forbidden_markers:
                to_strip = []
                for sname, sval in servers.items():
                    name_low = sname.lower()
                    val_low = " ".join(_iter_strings(sval)).lower()
                    if any(m in name_low or m in val_low for m in forbidden_markers):
                        to_strip.append(sname)
                for sname in to_strip:
                    servers.pop(sname, None)
                    changes.append(f"{sname}:removed-brand-wall")

        serialized = json.dumps(data, indent=2) + "\n"
        # Verify zero env secrets leaked by checking DECODED strings (immune to JSON escaping).
        # In cloud mode scan the ENTIRE file (including refusing embedded Postgres URIs in ALL cloud modes);
        # in local mode scan all newly generated bridge entries and report if a pre-existing static Composio header remains.
        if mode == "cloud":
            decoded_strings = list(_iter_strings(data))
            if any(sv in s for s in decoded_strings for sv in secret_values):
                raise RuntimeError("REFUSED: secret value detected in cloud mcp_config.json")
            if any("postgresql://" in s.lower() or "postgres://" in s.lower() for s in decoded_strings):
                raise RuntimeError("REFUSED: embedded postgres URI detected in cloud mcp_config.json")
        else:
            for name in ("offices", "composio"):
                if name in servers and f"{name}:bridge" in changes:
                    entry_strings = list(_iter_strings(servers[name]))
                    if any(sv in s for s in entry_strings for sv in secret_values):
                        raise RuntimeError(f"REFUSED: secret value detected in generated {name} entry")

        backup_path = None
        if changes and not dry_run:
            # Local mode keeps a 0600 backup; cloud mode does NOT copy stripped/cross-tenant configs on disk.
            backup_path = _write_atomic(target, serialized, backup=(mode == "local"))

        comp_entry = servers.get("composio")
        composio_static = bool(isinstance(comp_entry, dict) and isinstance(comp_entry.get("headers"), dict) and comp_entry["headers"])
        results.append({
            "path": str(target),
            "changes": changes,
            "written": bool(changes and not dry_run),
            "backup": str(backup_path) if backup_path else None,
            "servers": sorted(servers.keys()),
            "has_headers_helper": any(isinstance(v, dict) and "headersHelper" in v for v in servers.values()),
            "composio_uses_static_header": composio_static,
        })

    return {
        "mode": mode,
        "office_env": office_env,
        "dry_run": dry_run,
        "bridge": str(bridge_path),
        "targets": results,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=["local", "cloud"], required=True)
    ap.add_argument("--home", default=str(Path.home()))
    ap.add_argument("--bridge", help="Path to installed mcp_http_bridge.py")
    ap.add_argument("--headers-helper", help="Local mode: path to offices headers.sh")
    ap.add_argument("--office-env", default="babyak-lab", choices=["poppy", "babyak-lab", "territory", "base"])
    ap.add_argument("--force-composio", action="store_true", help="Rewrite composio to bridge even in local mode")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    home = Path(a.home)
    bridge = Path(a.bridge) if a.bridge else home / ".config" / "offices" / "mcp_http_bridge.py"
    rep = apply_config(
        home=home,
        mode=a.mode,
        bridge_path=bridge,
        office_env=a.office_env,
        helper_path=a.headers_helper,
        force_composio=a.force_composio,
        dry_run=a.dry_run,
    )
    print(json.dumps(rep, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
