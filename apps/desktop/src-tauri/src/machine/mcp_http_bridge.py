#!/usr/bin/env python3
"""Universal stdio <-> Streamable-HTTP MCP bridge for Antigravity and other harnesses.

Why it exists:
  Antigravity's `mcp_config.json` supports `url` + static `headers` or `command` + `args`,
  NOT Claude Code's `headersHelper` and NOT `${ENV_VAR}` interpolation inside HTTP headers.
  Putting bearer tokens or API keys directly into `mcp_config.json` would persist secrets
  on disk and break per-job tokens in cloud boxes.

  This stdlib-only bridge runs as a stdio MCP server whose config contains only this script
  path, the HTTPS endpoint URL, and credential SOURCES (env var names, secret file paths,
  or a local `headersHelper` executable). Secret values are read only at request time and
  are never placed in argv, written to disk, or logged.

Supported flags:
  --url https://host/mcp
  --header-env HEADER=VAR1[,VAR2,...]
      Resolve HEADER from the first non-empty env var in the comma-separated list.
  --bearer-env-or-file VAR1[,VAR2,...]:PATH1[,PATH2,...]
      Send `Authorization: Bearer <secret>` from the first non-empty env var, else the
      first readable non-empty file path. Either side of `:` may be empty.
  --header-env-default HEADER=VAR:DEFAULT
      For NON-SECRET metadata headers (e.g. `x-offices-caller=OFFICES_CALLER:generalist:t3`).
      Refuses secret-looking header names (`authorization`, `*key*`, `*token*`, `*secret*`, `cookie`).
  --headers-helper /abs/path/to/headers.sh
      Execute a local helper script that prints a JSON object of HTTP headers on stdout
      (same contract as Claude Code's `headersHelper`). Values are never logged.

Reads newline-delimited JSON-RPC from stdin and POSTs each message. JSON or SSE responses
are emitted as newline-delimited JSON. `Mcp-Session-Id` is preserved across calls.
Stdlib only.
"""
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.request

URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~/%-]*)?$")
HDR_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
ALLOWED_DEFAULT_HEADERS = frozenset({"x-offices-caller", "x-offices-office"})

_out = threading.Lock()
_sid = {"v": None}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects so Authorization / API keys / Mcp-Session-Id never cross origins."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"redirect refused ({code})", headers, fp)


def ssl_context():
    ca = os.environ.get("SSL_CERT_FILE") or ""
    if not ca and os.path.exists("/etc/ssl/certs/ca-certificates.crt"):
        ca = "/etc/ssl/certs/ca-certificates.crt"  # Boat images set SSL_CERT_FILE=""
    return ssl.create_default_context(cafile=ca or None)


def _split_vars(spec):
    items = [v.strip() for v in spec.split(",") if v.strip()]
    if not items or any(not VAR_NAME_RE.match(v) for v in items):
        raise SystemExit("invalid env var list: expected UPPER_SNAKE_CASE names")
    return items


def parse_args(argv):
    url = None
    header_envs = []       # list of (header_name, [var_names])
    bearer_sources = None  # ([var_names], [paths]) or None
    header_defaults = []   # list of (header_name, var_name, default_val)
    headers_helper = None

    i = 0
    while i < len(argv):
        flag = argv[i]
        if i + 1 >= len(argv):
            raise SystemExit(f"missing value for {flag}")
        val = argv[i + 1]
        i += 2
        if flag == "--url":
            url = val
        elif flag == "--header-env":
            if "=" not in val:
                raise SystemExit("bad --header-env (want HEADER=VAR1[,VAR2])")
            hname, vspec = val.split("=", 1)
            if not HDR_NAME_RE.match(hname):
                raise SystemExit("bad header name in --header-env")
            header_envs.append((hname, _split_vars(vspec)))
        elif flag == "--bearer-env-or-file":
            if ":" not in val:
                raise SystemExit("bad --bearer-env-or-file (want VAR1[,VAR2]:PATH1[,PATH2])")
            vspec, pspec = val.split(":", 1)
            vars_ = _split_vars(vspec) if vspec.strip() else []
            paths = [p.strip() for p in pspec.split(",") if p.strip()]
            if not vars_ and not paths:
                raise SystemExit("--bearer-env-or-file needs at least one env var or file path")
            bearer_sources = (vars_, paths)
        elif flag == "--header-env-default":
            if "=" not in val or ":" not in val.split("=", 1)[1]:
                raise SystemExit("bad --header-env-default (want HEADER=VAR:DEFAULT)")
            hname, rest = val.split("=", 1)
            vname, default_val = rest.split(":", 1)
            if not HDR_NAME_RE.match(hname) or not VAR_NAME_RE.match(vname):
                raise SystemExit("bad header or var name in --header-env-default")
            if hname.lower() not in ALLOWED_DEFAULT_HEADERS:
                raise SystemExit("header not in allowlist for --header-env-default (allowed: x-offices-caller, x-offices-office)")
            header_defaults.append((hname, vname, default_val))
        elif flag == "--headers-helper":
            if not val.startswith("/"):
                raise SystemExit("--headers-helper must be an absolute path")
            headers_helper = val
        else:
            raise SystemExit(f"unknown argument: {flag}")

    if not url or not URL_RE.match(url):
        raise SystemExit("--url must be a plain https URL (no query or credentials)")
    return {
        "url": url,
        "header_envs": header_envs,
        "bearer_sources": bearer_sources,
        "header_defaults": header_defaults,
        "headers_helper": headers_helper,
    }


def _read_first_file(paths):
    for raw in paths:
        p = Path(os.path.expanduser(raw))
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except OSError:
            continue
    return None


def _run_helper(helper_path, env):
    p = Path(helper_path)
    if not p.is_file():
        raise RuntimeError("headers helper not found")
    proc = subprocess.run(
        [str(p)],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"headers helper exited {proc.returncode}")
    data = json.loads(proc.stdout)
    if not isinstance(data, dict):
        raise RuntimeError("headers helper did not return a JSON object")
    out = {}
    for k, v in data.items():
        if isinstance(k, str) and HDR_NAME_RE.match(k) and isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def resolve_headers(cfg, env=None):
    env = os.environ if env is None else env
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "offices-mcp-bridge/1",
    }
    missing = []

    if cfg["headers_helper"]:
        headers.update(_run_helper(cfg["headers_helper"], env))

    for hname, vnames in cfg["header_envs"]:
        val = next((env.get(v, "").strip() for v in vnames if env.get(v, "").strip()), None)
        if val is None:
            missing.append("/".join(vnames))
        else:
            headers[hname] = val

    if cfg["bearer_sources"]:
        vnames, paths = cfg["bearer_sources"]
        secret = next((env.get(v, "").strip() for v in vnames if env.get(v, "").strip()), None)
        if not secret and paths:
            secret = _read_first_file(paths)
        if not secret:
            missing.append("bearer(" + "/".join(vnames + paths) + ")")
        else:
            headers["Authorization"] = f"Bearer {secret}"

    for hname, vname, default_val in cfg["header_defaults"]:
        val = env.get(vname, "").strip() or default_val
        if val:
            headers[hname] = val

    if missing:
        raise RuntimeError("credential source unavailable: " + ", ".join(missing))
    return headers


def emit(msg):
    with _out:
        sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def messages_from(body, ctype):
    text = body.decode("utf-8", "replace")
    if "text/event-stream" in ctype:
        out, data = [], []
        for line in text.splitlines() + [""]:
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line.strip() and data:
                out.append("\n".join(data))
                data = []
        return [json.loads(d) for d in out if d.strip()]
    if not text.strip():
        return []
    v = json.loads(text)
    return v if isinstance(v, list) else [v]


def post(cfg, ctx, msg, env=None):
    headers = resolve_headers(cfg, env=env)
    if _sid["v"]:
        headers["Mcp-Session-Id"] = _sid["v"]
    req = urllib.request.Request(
        cfg["url"],
        data=json.dumps(msg).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), _NoRedirect())
    with opener.open(req, timeout=600) as resp:
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            _sid["v"] = sid
        return messages_from(resp.read(), resp.headers.get("Content-Type", ""))


def handle(cfg, ctx, msg):
    rid = msg.get("id") if isinstance(msg, dict) else None
    try:
        for m in post(cfg, ctx, msg):
            emit(m)
    except urllib.error.HTTPError as e:
        if rid is not None:
            emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": f"upstream HTTP {e.code}"}})
    except Exception as e:
        if rid is not None:
            emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": f"bridge error: {type(e).__name__}"}})


def main(argv=None):
    cfg = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        resolve_headers(cfg)
    except RuntimeError as e:
        print(f"mcp_http_bridge: {e}", file=sys.stderr)
        return 2
    ctx = ssl_context()
    threads = []
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("method") == "initialize":
            handle(cfg, ctx, msg)  # serial so Mcp-Session-Id is established before fan-out
            continue
        t = threading.Thread(target=handle, args=(cfg, ctx, msg), daemon=True)
        t.start()
        threads.append(t)
        threads = [x for x in threads if x.is_alive()]
    for t in threads:
        t.join(timeout=600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
