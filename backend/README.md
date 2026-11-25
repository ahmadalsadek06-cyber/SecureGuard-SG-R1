# SecureGuard SG-R1 Event Backend

Independent FastAPI backend for the Webots SG-R1 simulation (no link to any other SecureGuard app).

```
Webots SG-R1 controller ──HTTPS + per-request HMAC──▶ this backend (SQLite) ──optional──▶ Elasticsearch (logs-secureguard.events-default) ──▶ Elastic Security
```

All events are **simulated**. Person identities are simulated tags from Webots Camera Recognition, not facial recognition. A "verified" record means the backend's HMAC shows it is unchanged since this backend accepted it — **not** that a simulated camera observation is physically authentic.

## Start (PowerShell, from `SecureGuard-Webots\backend`)

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m app.provision      # once; never overwrites existing secrets
.\.venv\Scripts\python -m app.serve          # https://127.0.0.1:8443 (loopback only)
```

`app.provision` creates `secrets\` (HMAC key, AES key, robot credential, dashboard token, localhost TLS). Its CA can only sign for localhost/127.0.0.1/::1 and its private key is discarded. Keep `secrets\` private and backed up; losing `aes.key` makes stored evidence unreadable.

Dashboard: open `https://localhost:8443/` and paste the token from `secrets\dashboard_token.txt`. To avoid browser certificate warnings, trust only the public `secrets\tls\ca.pem` for your user: `certutil -user -addstore Root secrets\tls\ca.pem`.

## Webots bridge

`controllers/sgr1_person_detection` sends events automatically when `backend\secrets\robot.json` exists (`SG_BACKEND_DISABLED=1` turns it off; `SG_BACKEND_URL` overrides `https://127.0.0.1:8443`). Sending runs on a background thread with a bounded queue (500), so WASD driving and detection never wait on the network. Events: `unauthorized_access` (30 s cooldown per person), `person_detected` (on status/zone change), `patrol_status` (robot zone change), `robot_status` (every 5 s sim time). `fire_detected`/`smoke_detected` are accepted by the API but the current Webots world has no fire/smoke source.

## API

| Method | Path | Auth |
|---|---|---|
| POST | `/api/v1/events` | Robot: `X-SG-Robot-Id`, `X-SG-Timestamp`, `X-SG-Nonce`, `X-SG-Signature` = HMAC-SHA-256(robot secret, `POST\n/api/v1/events\n<ts>\n<nonce>\n<sha256(body)>`) |
| GET | `/api/v1/events?limit=&event_type=` · `/api/v1/events/{id}` · `/api/v1/robots` · `/api/v1/audit` | `Authorization: Bearer <dashboard token>` |
| GET | `/healthz`, `/` (dashboard page) | none (no data) |

Protections: HTTPS only (HTTP → 426); ±120 s request clock skew; single-use nonces (replay → 409); per-client rate limit (20/s, burst 40 → 429); strict schema (IDs, ISO-8601 UTC offsets, event ≤24 h old and not in the future, severity floors, coordinate bounds, zones A1–C2/UNMAPPED, no extra fields, no NaN); `robot_id` must match the credential. Same event ID + same content is idempotent (200); different content → 409. Person identity and message are AES-256-GCM sealed (random 96-bit nonce, AAD = event ID); every record has an HMAC-SHA-256 tag checked on every read and by `/api/v1/audit`.

## Optional Elastic export (off by default)

Enabled only when both variables are set **in the backend's PowerShell window**. The key is typed hidden and is never stored or printed:

```powershell
$env:ELASTICSEARCH_URL = Read-Host "Your Elasticsearch HTTPS endpoint"
$secure = Read-Host "Elastic API key" -AsSecureString
$env:ELASTICSEARCH_API_KEY = [System.Net.NetworkCredential]::new("", $secure).Password
.\.venv\Scripts\python -m app.serve           # logs: Elastic export ENABLED
```

Needs an API key allowed to `create_doc` + `auto_configure` on `logs-secureguard.events-*`. Each verified event becomes one ECS document (`@timestamp`, `event.id/action/kind/category/type/severity`, `observer.name`, `user.id`, `labels.simulated`, `secureguard.*`) created with `_create/<event_id>`: 409 means already exported, so no duplicates. 408/429/5xx/network errors retry with exponential backoff; 400/401/403 wait 15 min; local records are never deleted. `robot_status` heartbeats stay local unless `SG_EXPORT_ROBOT_STATUS=true`. Close the window (or `Remove-Item Env:ELASTICSEARCH_API_KEY`) to clear the key.

## Live Webots → Elastic test

1. Window 1: start the backend with the Elastic variables (above).
2. Open `worlds\secureguard_person_detection.wbt` in Webots, click **Real-time**, focus the 3D view.
3. Drive SG-R1 (W, then W+A) to look through the Server Room door at **Visitor_01** → console shows `ALERT unauthorized_access`.
4. Dashboard: the event appears with integrity `verified`; the audit line shows `elastic: {"exported": N}`.
5. Kibana → Discover, data view `logs-*`: `data_stream.dataset : "secureguard.events" and event.action : "unauthorized_access"`.

## Tests

```powershell
.\.venv\Scripts\python -m pytest -q tests
```

Temp databases/secrets only; Elastic is mocked and real sends are blocked. Includes a real TLS round trip from the Webots bridge to uvicorn.

## Limitations

- Robot secret is a shared symmetric key on the same machine as Webots; no hardware attestation.
- Rate limiter and bridge queue are in memory (reset on restart); events still queued when Webots quits may be lost after a 2 s flush.
- Nonce table protects replays only within the ±120 s window; older requests fail the timestamp check.
- No key rotation workflow; changing `hmac.key` makes earlier records show `invalid`.
- Detection inherits Webots Recognition limits (approximate occlusion; partially visible people may be missed).
