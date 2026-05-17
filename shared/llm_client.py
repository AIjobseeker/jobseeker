"""Unified Claude client — picks the best available backend at runtime.

Priority (first one available wins):
  1. Internal SDK         — no API key needed; runtime auth handled by the SDK.
                            Enable by installing a package whose import name
                            is set in `JOBSEEKER_LLM_INTERNAL_PKG` (default
                            disables this path on most machines).
  2. Internal OAuth proxy — Anthropic SDK with custom `base_url` + bearer
                            token from a local OAuth CLI tool. Configure via:
                              ANTHROPIC_BASE_URL=<your internal proxy URL>
                              JOBSEEKER_INTERNAL_TOKEN_CLI=<path to a CLI that
                                                             prints a JWT>
                              JOBSEEKER_INTERNAL_OAUTH_AUDIENCE=<oauth client/audience>
                            Or set ANTHROPIC_AUTH_TOKEN directly.
  3. Direct Anthropic     — needs a real ANTHROPIC_API_KEY (sk-ant-...) + credit.
  4. Ollama               — local fallback. ANTHROPIC_API_KEY not needed.
                            Requires `--use-claude` to be FALSE.

Auto-refresh:
  When `ANTHROPIC_BASE_URL` is set AND a token-fetching CLI is configured,
  this module fetches a fresh OAuth token on demand and caches it in-memory
  until ~5 min before its JWT exp claim. ANTHROPIC_AUTH_TOKEN in the env is
  treated as a manual override that takes precedence over the CLI.

This module is intentionally vendor-agnostic. Configure your corporate /
internal proxy via env vars in your local `.env` (gitignored). Do not commit
internal endpoints, audience IDs, or CLI paths to source.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional


# ── Internal-OAuth token auto-fetch via a configured CLI ───────────────────
# The CLI is expected to print one of:
#   oauth-id <jwt>
#   oauth-id: <jwt>
#   <jwt>            (just the bare token on its own line)
# The CLI binary path is read from JOBSEEKER_INTERNAL_TOKEN_CLI; if unset we
# search PATH for the basename in JOBSEEKER_INTERNAL_TOKEN_CLI_NAME.

_TOKEN_CACHE: dict[str, object] = {"value": None, "expires_at": 0.0}


def _token_cli_extra_args() -> list[str]:
    """Caller can pass extra args to the token CLI via env var (space-sep)."""
    raw = os.environ.get("JOBSEEKER_INTERNAL_TOKEN_CLI_ARGS", "").strip()
    return raw.split() if raw else []


def _find_internal_token_cli() -> Optional[str]:
    """Locate the token-fetching CLI binary.

    Search order:
      1. JOBSEEKER_INTERNAL_TOKEN_CLI as an absolute or PATH-resolvable path
      2. PATH lookup for JOBSEEKER_INTERNAL_TOKEN_CLI_NAME
    """
    explicit = os.environ.get("JOBSEEKER_INTERNAL_TOKEN_CLI", "").strip()
    if explicit:
        if Path(explicit).is_file() and os.access(explicit, os.X_OK):
            return explicit
        resolved = shutil.which(explicit)
        if resolved:
            return resolved
    name = os.environ.get("JOBSEEKER_INTERNAL_TOKEN_CLI_NAME", "").strip()
    if name:
        return shutil.which(name)
    return None


def _decode_jwt_exp(token: str) -> Optional[float]:
    """Pull `exp` (epoch seconds) out of an unverified JWT. Returns None on error."""
    try:
        _, payload_b64, _ = token.split(".", 2)
        # JWT base64url: pad to multiple of 4
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        exp = payload.get("exp")
        return float(exp) if exp is not None else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _fetch_internal_token_via_cli(verbose: bool = False) -> Optional[str]:
    """Call the configured token CLI and return the OAuth token, or None.

    Set verbose=True (or env JOBSEEKER_DEBUG_TOKEN=1) to print why the fetch
    failed — usually one of: CLI not configured, non-zero exit, or output
    that didn't match the expected format.
    """
    debug = verbose or os.environ.get("JOBSEEKER_DEBUG_TOKEN", "").strip() == "1"

    cli = _find_internal_token_cli()
    if not cli:
        if debug:
            print(
                "[token] no internal token CLI configured. Set "
                "JOBSEEKER_INTERNAL_TOKEN_CLI to the absolute path of a CLI "
                "that prints a JWT, or JOBSEEKER_INTERNAL_TOKEN_CLI_NAME to "
                "look it up on PATH."
            )
        return None
    if debug:
        print(f"[token] using {cli}")

    try:
        proc = subprocess.run(
            [cli, *_token_cli_extra_args()],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        if debug:
            print(f"[token] subprocess failed: {type(exc).__name__}: {exc}")
        return None

    if proc.returncode != 0:
        if debug:
            print(f"[token] CLI exited {proc.returncode}")
            if proc.stderr.strip():
                print(f"[token] stderr: {proc.stderr.strip()[:500]}")
            if proc.stdout.strip():
                print(f"[token] stdout: {proc.stdout.strip()[:500]}")
        return None

    # Tolerate either `oauth-id <jwt>` or `oauth-id: <jwt>` or just <jwt>.
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("oauth-id"):
            rest = line[len("oauth-id"):].lstrip(": \t")
            if rest:
                return rest.split()[0].strip()
        # Some CLIs print just the JWT on its own line.
        if line.count(".") == 2 and len(line) > 80 and " " not in line:
            return line

    if debug:
        head = "\n".join(proc.stdout.splitlines()[:5])
        print(f"[token] no recognizable token in stdout. First 5 lines:\n{head}")
    return None


def _get_internal_token(force_refresh: bool = False) -> Optional[str]:
    """Resolve an internal-proxy bearer token. Order:
        1. ANTHROPIC_AUTH_TOKEN env var (manual override; user in control)
        2. cached value from a previous CLI fetch (until ~5 min before exp)
        3. fresh CLI fetch

    Returns None when no source is available.
    """
    manual = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    if manual:
        return manual

    now = time.time()
    cached = _TOKEN_CACHE.get("value")
    expires_at = float(_TOKEN_CACHE.get("expires_at") or 0)
    if not force_refresh and cached and now < expires_at - 300:  # 5min skew
        return str(cached)

    fresh = _fetch_internal_token_via_cli()
    if not fresh:
        return None

    exp = _decode_jwt_exp(fresh) or (now + 3000)  # fallback: 50 min
    _TOKEN_CACHE["value"] = fresh
    _TOKEN_CACHE["expires_at"] = exp
    return fresh


def _has_internal_sdk() -> bool:
    """Check whether an internal SDK (configured via env) is importable.

    The package name is read from JOBSEEKER_LLM_INTERNAL_PKG. If the env var
    is unset, this path is disabled. Catch BaseException because some
    internal SDKs use Python 3.10+ syntax that fails to import on 3.9 with
    TypeError rather than ImportError.
    """
    pkg = os.environ.get("JOBSEEKER_LLM_INTERNAL_PKG", "").strip()
    if not pkg:
        return False
    try:
        __import__(pkg)
        return True
    except BaseException:
        return False


def _has_internal_proxy() -> bool:
    """True iff ANTHROPIC_BASE_URL points anywhere AND we have a way to mint
    a token (env var or configured CLI)."""
    base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    if not base:
        return False
    if os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip():
        return True
    if os.environ.get("ANTHROPIC_EXTRA_HEADERS_JSON", "").strip():
        return True
    return _find_internal_token_cli() is not None


def _has_direct_anthropic() -> bool:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return bool(key) and not key.endswith("...") and len(key) >= 40 and key != "dummy"


def available_backend() -> str:
    """Return which backend will be used:
        'internal_sdk' | 'internal_proxy' | 'direct' | 'none'
    """
    if _has_internal_sdk():
        return "internal_sdk"
    if _has_internal_proxy():
        return "internal_proxy"
    if _has_direct_anthropic():
        return "direct"
    return "none"


def _claude_via_internal_sdk(prompt: str, model: str, max_tokens: int) -> str:
    """Call an internal SDK whose import path is given by env vars:
        JOBSEEKER_LLM_INTERNAL_PKG     — package name to import (e.g. "yoursdk")
        JOBSEEKER_LLM_INTERNAL_ENTRY   — dotted import path of a callable
                                          with signature (prompt, model, max_tokens)
                                          returning a string.

    The default entry point we try first is `<pkg>.ai.ask` if no override.
    """
    pkg = os.environ["JOBSEEKER_LLM_INTERNAL_PKG"].strip()
    entry = os.environ.get("JOBSEEKER_LLM_INTERNAL_ENTRY", f"{pkg}.ai.ask").strip()
    mod_name, _, attr = entry.rpartition(".")
    mod = __import__(mod_name, fromlist=[attr])
    fn = getattr(mod, attr)
    return fn(prompt=prompt, model=model, max_tokens=max_tokens)


def _build_anthropic_client(token: Optional[str] = None):
    """Build an anthropic.Anthropic client. Used for both internal-proxy mode
    (with `base_url` override + bearer token) and direct Anthropic.
    The `token` argument lets callers pass an explicit token (refresh+retry).
    """
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip() or "dummy"
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or None

    extra: dict[str, str] = {}
    auth_token = token or _get_internal_token() if base_url else None
    if auth_token:
        extra["Authorization"] = f"Bearer {auth_token}"
    raw_extra = os.environ.get("ANTHROPIC_EXTRA_HEADERS_JSON", "").strip()
    if raw_extra:
        try:
            extra.update(json.loads(raw_extra))
        except json.JSONDecodeError:
            pass

    kwargs: dict[str, object] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    if extra:
        kwargs["default_headers"] = extra
    return anthropic.Anthropic(**kwargs)


def _claude_via_anthropic_sdk(
    prompt: str,
    model: str,
    max_tokens: int,
    system: Optional[str] = None,
) -> str:
    """Shared dispatch for internal proxy and direct Anthropic.

    On 401 (token expired), force-refresh the configured CLI token and retry once.
    """
    import anthropic

    msg_kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        msg_kwargs["system"] = system

    client = _build_anthropic_client()
    try:
        resp = client.messages.create(**msg_kwargs)
    except anthropic.AuthenticationError:
        # Token expired mid-flight. Refresh once and retry.
        fresh = _get_internal_token(force_refresh=True)
        if not fresh:
            raise
        client = _build_anthropic_client(token=fresh)
        resp = client.messages.create(**msg_kwargs)
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def claude_chat(
    prompt: str,
    *,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 4000,
    system: Optional[str] = None,
) -> str:
    """One call, picks the best backend. Raises RuntimeError if none available.

    Caller can force a backend via:
        export JOBSEEKER_LLM_BACKEND=internal_sdk|internal_proxy|direct
    """
    forced = os.environ.get("JOBSEEKER_LLM_BACKEND", "").strip().lower()
    backend = forced or available_backend()

    if backend == "internal_sdk":
        if not _has_internal_sdk():
            raise RuntimeError(
                "JOBSEEKER_LLM_BACKEND=internal_sdk but the configured package "
                "(JOBSEEKER_LLM_INTERNAL_PKG) isn't importable on this Python."
            )
        return _claude_via_internal_sdk(prompt, model, max_tokens)

    if backend in ("internal_proxy", "direct"):
        return _claude_via_anthropic_sdk(prompt, model, max_tokens, system=system)

    raise RuntimeError(
        "No Claude backend available. Pick one:\n"
        "  - Internal SDK:    set JOBSEEKER_LLM_INTERNAL_PKG to an importable "
        "package providing `<pkg>.ai.ask(prompt, model, max_tokens)`\n"
        "  - Internal proxy:  set ANTHROPIC_BASE_URL to your proxy URL and one of:\n"
        "                       ANTHROPIC_AUTH_TOKEN=<jwt>\n"
        "                       JOBSEEKER_INTERNAL_TOKEN_CLI=<path to CLI>\n"
        "  - Direct Anthropic: set ANTHROPIC_API_KEY (sk-ant-...) with credit\n"
        "  - Skip Claude entirely: drop --use-claude flag and use Ollama"
    )
