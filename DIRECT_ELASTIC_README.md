# Direct mode: SG-R1 → Elasticsearch (without FastAPI or SQLite)

```
backend mode (default):  Webots SG-R1 ──HTTPS+HMAC──▶ FastAPI ─▶ SQLite ─▶ outbox ─▶ Elasticsearch ─▶ Detection rule
elastic mode (optional): Webots SG-R1 ─────────────HTTPS (ApiKey)──────────────────▶ Elasticsearch ─▶ Detection rule
```

Choose the mode with **`SG_EVENT_DESTINATION`** in the environment that **launches Webots**:

- `backend` (default, or when the variable is missing/unknown): the existing `backend_bridge.py`. Nothing changed.
- `elastic`: the new `controllers/sgr1_person_detection/direct_elastic.py`.

## What stays the same

- **Robot behaviour:** driving (WASD and combinations), camera, NPC recognition, zone authorization, the 30 s alert cooldown and the world layout are untouched. Only the event destination changes.
- **Document format:** documents use the backend exporter's ECS mapping (it is imported from `backend/app/elastic.py`). The data stream (`logs-secureguard.events-default`), `event.action` values, `event.id`, `@timestamp`, `observer.name` (robot), `user.id` (person), `secureguard.zone`, `secureguard.person.classification`, `event.severity` and `labels.simulated` are identical. Your existing detection rule does not need changes.
- **The one marked difference:** `secureguard.integrity` is `not_verified_direct_mode`, and `secureguard.transport` is `direct_robot_to_elastic`.
- **`robot_status` heartbeats** are not exported, which is the same default as the backend. Set `SG_EXPORT_ROBOT_STATUS=true` to include them.

## Dedicated least-privilege API key (create it yourself in Kibana)

**Stack Management → API keys → Create API key**, with this role descriptor (create documents in this one data stream only):

```json
{
  "secureguard_direct_writer": {
    "indices": [
      { "names": ["logs-secureguard.events-default"], "privileges": ["create_doc"] }
    ]
  }
}
```

- **If the data stream does not exist yet,** add `"auto_configure"` to the privileges, still limited to the same name.
- **Do not use** an administrator or "superuser" key.
- **For the optional visibility check,** create a *separate* read-only key (`"privileges": ["read"]` on `logs-secureguard.events-*`). The writer key cannot search.

## Start in direct mode (PowerShell)

Open PowerShell in `<your-clone-directory>`. The key is typed hidden and exists only in this window:

```powershell
$env:SG_EVENT_DESTINATION = "elastic"
$env:ELASTICSEARCH_URL = Read-Host "Your Elasticsearch HTTPS endpoint"
$secure = Read-Host "Elastic WRITER API key (create_doc only)" -AsSecureString
$env:ELASTICSEARCH_API_KEY = [System.Net.NetworkCredential]::new("", $secure).Password
& "C:\Program Files\Webots\msys64\mingw64\bin\webots.exe" worlds\secureguard_person_detection.wbt
```

- **Webots must be started from this window,** so the controller inherits the variables. A Webots opened from the Start menu stays in backend mode.
- **Console line to check:** `SG-R1 direct Elastic: enabled -> https://…/logs-secureguard.events-default (TLS verified)`.
- **Each delivered event prints:** `SG-R1 direct Elastic: created <event_type> SGR1-… (HTTP 201)`.
- **When finished,** close the window or run `Remove-Item Env:ELASTICSEARCH_API_KEY`.

## One live verification event

In the same configured window:

```powershell
python scripts\live_direct_elastic_event.py
```

- **What it does:** runs Webots headless for 4 s with SG-R1 facing Visitor_01, which produces exactly one `unauthorized_access`.
- **What it prints:** the `HTTP 201` line and the event ID. It refuses to run if the variables are missing.
- **Visibility check:** optionally set `$env:ELASTICSEARCH_READ_API_KEY` (read-only key, entered the same hidden way) and the script also searches for the document.
- **Without a read key,** use Kibana Discover → data view `logs-*` → `event.id : "<printed id>"`.

## Delivery guarantees

| Situation | Behaviour |
|---|---|
| Normal | `submit()` only enqueues (bounded queue of 500), and one background thread sends. Robot motion never waits for the network. |
| Before each send | The event is written to a durable spool (`data/direct_elastic_spool/<event_id>.json`, override with `SG_DIRECT_SPOOL_DIR`). The spool contains event data only, never the API key. It is deleted after Elasticsearch confirms. |
| Retries / duplicates | `PUT logs-secureguard.events-default/_create/<event_id>` with a stable ID. A retry cannot create a second document. **409 = already present** is treated as delivered. |
| Network error, 408, 429, 5xx | Exponential backoff 1 s → 2 s → 4 s … max 60 s. The same event is retried, and the failure is reported once. |
| 401 / 403 | The key or privilege is wrong. The event stays in the spool, is retried every 60 s, and is reported once. |
| Other 4xx (e.g. 400) | Moved to `data/direct_elastic_spool/rejected/` and reported. Never silent. |
| Queue full | The event is written to the spool on disk and reported. If the spool also reaches 5000 files, the event is dropped and the drop is **counted and reported**. |
| Webots closed with events pending | They stay in the spool and are sent at the next start, which logs `recovered from spool`. |
| TLS | Always verified (`ssl.create_default_context`). `SG_ELASTIC_CA_FILE` can add a private CA for testing, but verification is never disabled. |

## Security trade-offs of direct mode

| Protection | backend mode | elastic (direct) mode |
|---|---|---|
| Robot request authentication (per-request HMAC, nonce, ±120 s window) | Yes | **No.** Only the Elastic API key authenticates the robot. |
| Server-side schema validation (Pydantic) | Yes | **Partial.** Light local checks in the controller only. |
| Rate limiting at a server you control | Yes | No (Elastic Cloud's own limits apply). |
| Local SQLite record of every event | Yes | **No.** Only the pending-event spool. |
| AES-256-GCM encryption of person identity at rest | Yes | **No.** Person data is stored in Elasticsearch, protected by Elastic's own access control and encryption. |
| HMAC integrity tag and local audit (`/api/v1/audit`) | Yes | **No.** `secureguard.integrity = not_verified_direct_mode`. |
| Local dashboard | Yes | **No.** Use Kibana. |
| Where the cloud credential lives | Backend process only | **Robot / Webots process** environment, so the robot machine must be trusted. |
| Moving parts | Webots + FastAPI + SQLite | Webots only |

**Use backend mode** when you need local evidence, integrity auditing and the dashboard. **Use direct mode** for a simpler demo path to Elastic Security, with a least-privilege writer key.

## Tests

```powershell
cd backend; .\.venv\Scripts\python -m pytest -q tests/test_direct_elastic.py     # mocked Elastic, no network
cd ..; backend\.venv\Scripts\python scripts\test_direct_elastic_webots.py        # Webots + local fake HTTPS Elastic
```
