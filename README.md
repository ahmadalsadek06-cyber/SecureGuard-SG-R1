# SecureGuard SG-R1

A cybersecurity capstone simulation of a four-wheel university security patrol robot in **Webots R2025a**. SG-R1 combines manual skid-steer driving, camera-based recognition of simulated personnel, individual zone authorization and direct HTTPS event delivery to Elasticsearch Cloud and Elastic Security.

This repository contains source code and the assets required to open the simulation. Credentials, cloud deployment configuration, databases, spool records, logs, local environments and private report screenshots are deliberately excluded.

## Facility and robot

The approximately 24 × 16 m university facility contains six zones:

| Zone | Area |
|---|---|
| A1 | Reception |
| A2 | Main Entrance |
| B1 | Central Corridor |
| B2 | Warehouse |
| C1 | Research Laboratory |
| C2 | Server Room |

The custom `protos/SecureGuardSGR1.proto` robot is approximately 0.70 × 0.50 × 0.90 m, with four motorized wheels, real collision geometry, a front camera and distance sensors. Lightweight shapes and a 160 × 120 camera image reduce graphics workload. World coordinates use an XY floor plane and Z height.

Open **`worlds/secureguard_person_detection.wbt`** for the principal demonstration. Other worlds preserve earlier facility, E-puck, large-robot and security-cabin stages. Generated world and texture files required to run Webots are included; runtime reports and editor state are not.

## Local setup

1. Install Webots **R2025a** and Python **3.11** (the evaluated Python version was 3.11.2).
2. Clone this repository using your authorized GitHub account and enter its directory.
3. Configure Webots to use your installed Python interpreter if necessary.
4. For an offline simulation with no backend or cloud delivery, start Webots from PowerShell:

```powershell
$env:SG_EVENT_DESTINATION = "backend"
$env:SG_BACKEND_DISABLED = "1"
& "C:\Program Files\Webots\msys64\mingw64\bin\webots.exe" worlds\secureguard_person_detection.wbt
```

Use Webots **Real-time** mode and focus the 3D view. No local API key, real database or backend provisioning is needed for this offline demonstration. The standard-library controller uses Webots' own Python `controller` module; it is not a normal standalone Python program.

## Manual movement

| Input | Action |
|---|---|
| W / S | Forward / reverse |
| A / D | Left / right pivot when stationary |
| W+A / W+D | Forward while steering |
| S+A / S+D | Reverse while steering with car-like reverse yaw |
| Space | Brake |
| C | Toggle camera and recognition in the person-detection world |
| Q / E / Z / X | Single-key alternatives to WA / WD / SA / SD |

The controller drains each keyboard sample, tolerates short input dropouts and ramps acceleration and braking. Forward command speed is 1.0 m/s and reverse 0.6 m/s; wheel commands are constrained to motor limits. Release movement keys to brake. Distance sensors do **not** implement autonomous collision avoidance. Physical keyboard rollover and focus can affect simultaneous input; single-key alternatives remain available.

## Person recognition and zone authorization

Three lightweight NPCs use Webots Camera Recognition tags: `Employee_01`, `Student_01` and `Visitor_01`. Recognition observes objects within the camera's view and configured 10 m range; Supervisor positions are used for zone mapping only after an object is observed. This is **not AI face recognition or facial identity verification**.

`controllers/sgr1_person_detection/identities.json` contains simulated roles, individual allowed zones, room rectangles and the 30-second simulation-time alert cooldown. Known identities are not automatically authorized everywhere. For example, Visitor_01 is unauthorized in C2. Unknown identities or unmapped locations are handled separately by the policy logic.

The Webots console reports identity, authorization, zone and distance. A visible policy violation generates `unauthorized_access`. NPC positions can be edited using their named Solid nodes in the Webots scene tree; save a separate world for experiments. Keep doorways and robot spawn positions unobstructed. Builder scripts can regenerate development assets and may overwrite generated files, so use a separate checkout before regeneration.

## Direct Elasticsearch integration

```text
Webots SG-R1 → direct_elastic.py → Elasticsearch Cloud → Elastic Security
```

Direct mode requires an Elasticsearch deployment and a suitable data stream/template for `logs-secureguard.events-default`. Configure a dedicated writer restricted to the required stream (`create_doc`; provisioning may additionally require `auto_configure`). Create an appropriate Elastic Security rule for `event.action: unauthorized_access`; alert creation is not automatic merely because a document was indexed.

Use process environment variables. Enter the key privately at the prompt, never in a command literal or tracked file:

```powershell
$env:SG_EVENT_DESTINATION = "elastic"
$env:ELASTICSEARCH_URL = Read-Host "Your Elasticsearch HTTPS endpoint"
$secure = Read-Host "Dedicated Elasticsearch writer API key" -AsSecureString
$env:ELASTICSEARCH_API_KEY = [System.Net.NetworkCredential]::new("", $secure).Password
& "C:\Program Files\Webots\msys64\mingw64\bin\webots.exe" worlds\secureguard_person_detection.wbt
# After Webots has closed:
Remove-Item Env:ELASTICSEARCH_API_KEY -ErrorAction SilentlyContinue
```

The background exporter queues events, writes a local pending-event spool and sends ECS documents over certificate-verified HTTPS. Stable `event.id` values and the `_create` endpoint handle retries: HTTP 201 means created and HTTP 409 means already present. Transient errors retry with backoff; HTTP 401/403 retains pending events. The spool contains event data and must also be kept private.

Use the exact accepted `event.id` to correlate a Discover record and the alert's **Source event**. Timestamps or alert counts alone are not enough. See [direct integration details](DIRECT_ELASTIC_README.md). The optional [legacy backend](backend/README.md) is a separate mode; `backend/app/elastic.py` is also reused for ECS mapping by direct mode and must remain present.

## Testing and evaluation

The capstone evaluation records **14 PASS, 1 BLOCKED, 0 FAIL and 0 NOT RUN** across 15 scenarios. These are documented evaluation results, not a promise that every installation will reproduce them. Tests were not rerun for repository publication.

| Scenario | Result |
|---|---|
| TC-01–06: driving, authorization, range/FOV, occlusion, cooldown | PASS in the documented local scope |
| TC-07: cloud acceptance | PASS |
| TC-08: cloud authentication rejection | PASS for rejection handling and reported retention; exact key expiry unverified |
| TC-09: cloud spool recovery | BLOCKED against exact recovered-ID correlation and final spool-clearance criteria; recovery activity and subsequent HTTP 201 were observed |
| TC-10–12: duplicate, transient failure and TLS checks | PASS against controlled local services |
| TC-13–14: exact-ID Discover and High-alert correlation | PASS for one correlated cloud event |
| TC-15: simulation and integrity metadata | PASS in captured local documents and mapper tests |

Supporting prior execution: 56 Python tests passed (including 15 direct-exporter tests), 11 policy logic checks, 16 Webots recognition checks, 18 driving checks, two negative-visibility checks, 12 Webots-to-local-HTTPS integration checks and eight additional local HTTPS fault checks. These counts overlap in coverage and should not be added into a single independent scenario count. The extra fault-test harness and genuine private evidence remain with the capstone submission, outside this source repository.

For local development, install test dependencies in an isolated environment:

```powershell
python -m venv backend\.venv
backend\.venv\Scripts\python -m pip install -r backend\requirements-dev.txt
backend\.venv\Scripts\python -m pytest backend\tests
$env:SG_EVENT_DESTINATION = "backend"
$env:SG_BACKEND_DISABLED = "1"
backend\.venv\Scripts\python scripts\test_person_detection.py
backend\.venv\Scripts\python scripts\test_sgr1_driving.py
backend\.venv\Scripts\python scripts\test_direct_elastic_webots.py
```

Webots runtime scripts default to the standard Windows installation path; inspect their CLI options for other installations. Maintained test scripts use temporary worlds and local test services; generated results are ignored by Git. Scripts named `live_*` are deliberate live-integration tools, **not** offline unit tests. Do not run them without your own authorized configuration; they can submit real cloud records. Generators may require Pillow for texture creation, while the checked-in assets can be opened without regeneration.

## Security and simulation limitations

- Only simulated identities and security events are modeled. Selected fixed poses were tested; recognition accuracy under all occlusions and real-world conditions is not established.
- Direct mode does not provide the optional backend's HMAC request validation, SQLite storage, AES-GCM evidence protection or local integrity audit. Documents truthfully carry `secureguard.integrity = not_verified_direct_mode`.
- A cloud HTTP 401 does not prove an expired key. A recovery count does not prove which exact event was recovered. These evidence limits are retained in the evaluation.
- Driving checks used deterministic keyboard injection in real Webots physics, not an exhaustive human-keyboard rollover study. No autonomous navigation or safety certification is claimed.
- The correlated source/alert timestamps differ by 40.508 seconds in one captured case. This is not camera-to-alert latency, a repeated benchmark or an optimization gain.
- API keys exist only in the configured process environment. Do not commit credentials, private certificates, local deployment details, logs, spool records or database files. This repository is intended to remain **private** unless its owner explicitly approves otherwise.

## Source layout and attribution

`controllers/` contains robot control, recognition and event export; `protos/` contains robot assets; `worlds/` contains runnable scenes and required textures; `scripts/` contains generators and maintained test tools; `backend/` contains the optional legacy service, shared mapping and Python tests.

Bundled Webots assets retain their upstream notices; see [vendor attribution](protos/vendor/README.md). Their licenses remain applicable. No new blanket license is assigned to third-party assets by this repository.
