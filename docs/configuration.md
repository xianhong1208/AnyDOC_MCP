# Configuration

[繁體中文](configuration.zh-TW.md) · [README](../README.md)

Everything is in [`config/config.yaml`](../config/config.yaml) and reads environment variables with `${VAR:-default}`. [`.env.example`](../.env.example) lists them:

| Variable | Default | Description |
|---|---|---|
| `SERVER_HOST` / `SERVER_PORT` | `0.0.0.0` / `5055` | Bind address. |
| `MCP_GATE_URL` | `http://localhost:4568` | The MCP Gate that issues tokens (`auth.issuer`). The pre-rename `MCP_CENTER_URL` is still read when this is unset. |
| `MORPHYARD_BASE_URL` | `http://127.0.0.1:5055` | The address this server is reached at. The token audience is `<MORPHYARD_BASE_URL>/mcp` and must equal the service registered in MCP Gate; it also appears in the protected-resource metadata. |
| `AUTH_ENABLED` | `true` | Set to `false` only for local experiments. |
| `MAX_INPUT_MB` | `50` | Per-file limit; also raises the MCP transport body limit (×1.5). |
| `ENGINE_TIMEOUT` | engine default | Seconds per engine call, applied to every engine. Unset keeps each engine's own limit: LibreOffice 180, Pandoc 120, OCR 300. |
| `LOG_LEVEL` | `INFO` | |

In the YAML, `auth.required_scopes` lets you demand a scope such as `mcp:tools:invoke` on every token, and `auth.audience` overrides the derived audience for the rare case where the registered resource URI is not `<base_url>/mcp`.

## Token revocation

Morphyard verifies MCP Gate tokens offline, so by default a token revoked in MCP Gate keeps working here until it expires — and a personal access token can live up to a year. Turn on the **revocation feed** and Morphyard polls MCP Gate's `POST /oauth/revoked` in the background (every `poll_interval_sec`, with jitter and error backoff), keeps the revoked-but-unexpired `jti`s in memory, and answers a listed token on `/mcp` with 401 `invalid_token`. A revocation takes effect within about one poll interval.

| Variable / setting | Default | Description |
|---|---|---|
| `MORPHYARD_REVOCATION_ENABLED` (`auth.revocation.enabled`) | `false` | Turn the feed on. Off = nothing is polled. |
| `MORPHYARD_REVOCATION_CLIENT_ID` | — | The resource-server client registered in MCP Gate (below). |
| `MORPHYARD_REVOCATION_CLIENT_SECRET` | — | Its secret. In the YAML it **must** stay an `${ENV_VAR}` reference; a literal value refuses to start. |
| `MORPHYARD_REVOCATION_POLL_INTERVAL` (`poll_interval_sec`) | `30` | Seconds between polls (5–3600). |
| `MORPHYARD_REVOCATION_FAIL_MODE` (`fail_mode`) | `open` | When MCP Gate is unreachable: `open` keeps accepting tokens and logs loudly; `closed` rejects every token once no poll has succeeded for `stale_after_intervals` × `poll_interval_sec` (YAML, default 4), and until the first poll after startup succeeds. |

The first poll fetches the whole list; later polls ask only for revocations since the previous one (with a 30 s overlap), and a full fetch repeats every hour. Entries are dropped once the token would have expired anyway. Startup logs whether the feed is enabled, and `GET /health` reports it under `revocation` (`last_success`, `revoked_count`, `consecutive_failures`, `last_error`, `stale`); a stale feed turns `status` into `degraded`.

**Setting it up** (once per deployment):

1. **Register a confidential client in MCP Gate.** Console → *OAuth Clients* → *Register trusted client*. Name it e.g. `Morphyard revocation feed`; leave *Redirect URIs* empty; under *Grant types* select only `client_credentials`; *Token endpoint authentication*: **Client secret (POST body)** (HTTP Basic works too — MCP Gate accepts either); *Resource server for*: **Morphyard**. Register, then copy the `client_id` (`mcpc_…`) and the client secret — the secret is shown once.
2. **Bind it to Morphyard** — already done if you picked *Resource server for* in step 1. Otherwise, on the client's row click the server icon (*Resource server…*), choose the Morphyard service and *Save*; or call `PUT /api/oauth/clients/<client_id>/resource-server` with `{"service_id": "<Morphyard service id>"}` as a console admin. The row then reads *Resource server for Morphyard*. The feed lists only tokens whose audience is that service's audience, so it must equal `<MORPHYARD_BASE_URL>/mcp`.
3. **Give Morphyard the credentials** in its environment (or `.env`), then restart:
   ```bash
   MORPHYARD_REVOCATION_ENABLED=true
   MORPHYARD_REVOCATION_CLIENT_ID=mcpc_...
   MORPHYARD_REVOCATION_CLIENT_SECRET=...
   ```
4. **Check it.** The startup log says `MCP Gate revocation feed: ENABLED …`; `curl -s localhost:5055/health` shows a recent `revocation.last_success`. Revoke a test token in MCP Gate; within `poll_interval_sec` a call to `/mcp` with it gets 401. `HTTP 401: invalid_client` in `last_error` means wrong credentials; `HTTP 403: unauthorized_client` means the client is not bound to a server.

## Renamed variables

Morphyard was called AnyDoc before 1.2.2. The old variable names are no longer read, so rename them in your environment or `.env` when you upgrade:

| Before 1.2.2 | Now |
|---|---|
| `ANYDOC_BASE_URL` | `MORPHYARD_BASE_URL` |
| `ANYDOC_REVOCATION_ENABLED` | `MORPHYARD_REVOCATION_ENABLED` |
| `ANYDOC_REVOCATION_CLIENT_ID` | `MORPHYARD_REVOCATION_CLIENT_ID` |
| `ANYDOC_REVOCATION_CLIENT_SECRET` | `MORPHYARD_REVOCATION_CLIENT_SECRET` |
| `ANYDOC_REVOCATION_POLL_INTERVAL` | `MORPHYARD_REVOCATION_POLL_INTERVAL` |
| `ANYDOC_REVOCATION_FAIL_MODE` | `MORPHYARD_REVOCATION_FAIL_MODE` |
