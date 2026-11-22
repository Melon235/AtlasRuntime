"""Direct-connection policy for the local OpenSandbox endpoint.

The Windows host runs AtlasRuntime; OpenSandbox runs in WSL. A VPN client publishes a WinINET
system proxy that httpx honours by default, so a request to a WSL address can be sent to the VPN
proxy and time out. The sandbox host must bypass that proxy while the provider keeps using it.

The bypass is derived from the *current* ``OPEN_SANDBOX_ENDPOINT``: no address is hard-coded, and a
changed WSL IP is picked up on the next call. Existing user ``NO_PROXY`` entries are preserved.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

DEFAULT_ENDPOINT = "localhost:8080"

#: Classification codes for the runtime-equivalent health probe.
SANDBOX_HEALTH_READY = "SANDBOX_HEALTH_READY"
SANDBOX_ENDPOINT_UNREACHABLE = "SANDBOX_ENDPOINT_UNREACHABLE"
SANDBOX_HTTP_PROXY_MISROUTED = "SANDBOX_HTTP_PROXY_MISROUTED"
SANDBOX_HEALTH_TIMEOUT = "SANDBOX_HEALTH_TIMEOUT"
SANDBOX_HEALTH_BAD_RESPONSE = "SANDBOX_HEALTH_BAD_RESPONSE"

LOOPBACK = ("localhost", "127.0.0.1", "::1")


def sandbox_endpoint() -> tuple[str, str]:
    """Return ``(endpoint, source)`` for the current process. Never cached across calls."""

    raw = os.environ.get("OPEN_SANDBOX_ENDPOINT")
    if raw and raw.strip():
        return raw.strip(), "env:OPEN_SANDBOX_ENDPOINT"
    return DEFAULT_ENDPOINT, "default"


def endpoint_host(endpoint: str) -> str:
    """Extract the host from ``host:port``, ``scheme://host:port`` or a bracketed IPv6 literal."""

    text = endpoint.strip()
    if "://" not in text:
        text = "//" + text
    parsed = urlsplit(text)
    host = parsed.hostname or ""
    return host


def _split_entries(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def merge_bypass(existing: str | None, hosts: Iterable[str]) -> list[str]:
    """Merge hosts into an existing NO_PROXY list, preserving order and user entries."""

    entries = _split_entries(existing)
    lowered = {item.lower() for item in entries}
    for host in hosts:
        host = host.strip()
        if not host:
            continue
        # NO_PROXY matches hosts, not schemes; a port here would never match.
        if host.lower() not in lowered:
            entries.append(host)
            lowered.add(host.lower())
    return entries


def bypass_hosts(endpoint: str | None = None) -> list[str]:
    endpoint = endpoint or sandbox_endpoint()[0]
    host = endpoint_host(endpoint)
    hosts = list(LOOPBACK)
    if host and host not in hosts:
        hosts.insert(0, host)
    return hosts


def apply_proxy_bypass(endpoint: str | None = None) -> dict[str, Any]:
    """Add the sandbox host to NO_PROXY/no_proxy in this process. Provider settings untouched."""

    if endpoint:
        endpoint, source = endpoint, "argument"
    else:
        endpoint, source = sandbox_endpoint()
    hosts = bypass_hosts(endpoint)
    before = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    entries = merge_bypass(before, hosts)
    value = ",".join(entries)
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value
    return {
        "endpoint": endpoint,
        "endpoint_source": source,
        "host": endpoint_host(endpoint),
        "bypass": value,
        "added": [item for item in entries if item not in _split_entries(before)],
        "preserved_existing": _split_entries(before),
        "proxy_env": {
            name: os.environ.get(name)
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
        },
    }


def probe_health(endpoint: str | None = None, *, timeout: float = 5.0) -> dict[str, Any]:
    """Probe the endpoint exactly as the runtime would: httpx with its default environment trust."""

    import httpx

    bypass = apply_proxy_bypass(endpoint)
    url = f"http://{bypass['endpoint']}/health"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url)
    except httpx.ProxyError as error:
        return _result(False, SANDBOX_HTTP_PROXY_MISROUTED, bypass, detail=str(error))
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException) as error:
        return _result(False, SANDBOX_HEALTH_TIMEOUT, bypass, detail=str(error))
    except (httpx.ConnectError, httpx.UnsupportedProtocol) as error:
        return _result(False, SANDBOX_ENDPOINT_UNREACHABLE, bypass, detail=str(error))
    except Exception as error:  # noqa: BLE001 - a probe must classify, not raise
        return _result(
            False, SANDBOX_ENDPOINT_UNREACHABLE, bypass, detail=f"{type(error).__name__}: {error}"
        )

    if response.status_code in {407, 502, 504}:
        return _result(
            False,
            SANDBOX_HTTP_PROXY_MISROUTED,
            bypass,
            status=response.status_code,
            detail="the request was answered by a proxy, not by OpenSandbox",
        )
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        payload = {}
    if (
        response.status_code == 200
        and isinstance(payload, dict)
        and payload.get("status") == "healthy"
    ):
        return _result(True, SANDBOX_HEALTH_READY, bypass, status=200, detail="healthy")
    return _result(
        False,
        SANDBOX_HEALTH_BAD_RESPONSE,
        bypass,
        status=response.status_code,
        detail=json.dumps(payload)[:200] if payload else "response is not a health payload",
    )


def probe_runtime_health(endpoint: str | None = None) -> dict[str, Any]:
    """Probe through the frozen OpenSandbox provider adapter, the same client ``run_goal`` uses.

    Only the adapter's ``health()`` path is exercised: no sandbox is created and no provider/model
    request is made.
    """

    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from atlasos.sandbox.opensandbox_provider import OpenSandboxOptions, OpenSandboxProvider

    if endpoint:
        resolved, source = endpoint, "argument"
    else:
        resolved, source = sandbox_endpoint()
    bypass = apply_proxy_bypass(resolved)

    def _health() -> Any:
        provider = OpenSandboxProvider(OpenSandboxOptions(endpoint=resolved))
        return asyncio.run(provider.health())

    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            health = _health()
        else:
            # Called from the live async path: run the SDK's own probe on a private loop.
            with ThreadPoolExecutor(max_workers=1) as pool:
                health = pool.submit(_health).result()
    except Exception as error:  # noqa: BLE001 - a probe must classify, not raise
        return _result(
            False,
            SANDBOX_ENDPOINT_UNREACHABLE,
            bypass,
            detail=f"{type(error).__name__}: {error}",
        )
    if health.status == "healthy":
        return _result(True, SANDBOX_HEALTH_READY, bypass, status=200, detail=health.message)
    message = str(health.message)
    if "ReadTimeout" in message or "Timeout" in message:
        code = SANDBOX_HEALTH_TIMEOUT
    elif "Proxy" in message:
        code = SANDBOX_HTTP_PROXY_MISROUTED
    elif "healthy" in message or "health status" in message:
        code = SANDBOX_HEALTH_BAD_RESPONSE
    else:
        code = SANDBOX_ENDPOINT_UNREACHABLE
    return _result(False, code, bypass, detail=f"{health.status}: {message}")


def _result(
    ok: bool,
    code: str,
    bypass: dict[str, Any],
    *,
    status: int | None = None,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "ok": ok,
        "code": code,
        "status_code": status,
        "detail": detail,
        "endpoint": bypass["endpoint"],
        "endpoint_source": bypass["endpoint_source"],
        "host": bypass["host"],
        "bypass": bypass["bypass"],
        "added": bypass["added"],
        "preserved_existing": bypass["preserved_existing"],
        "proxy_env": bypass["proxy_env"],
        "used_proxy_bypass": bypass["host"] in _split_entries(bypass["bypass"]),
    }
