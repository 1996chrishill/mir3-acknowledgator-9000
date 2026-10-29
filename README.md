# mir3-acknowledgator-9000

Small API server that logs into your organization's MIR3 recipient portal with a headless browser and acknowledges your pending notifications.

> ⚠️ Acking a notification stops its escalation. Only run this if your team/policy allows it.

## API

| Method | Path | Does |
|---|---|---|
| GET | `/health` | Liveness, plus whether a run is in progress |
| GET | `/pending` | Lists pending notifications (no changes) |
| POST | `/ack-all` | Acks every pending notification |
| POST | `/ack/{event_hist_id}` | Acks one notification (ID from `/pending`) |
| GET | `/history?limit=N` | Past ack attempts from `history.json`, newest first (`limit` optional) |

One call = one run. Calls wait and return the result.

### Status codes

| Code | Meaning |
|---|---|
| 200 | Success (`"status": "success"`) |
| 409 | Busy: another call is already running |
| 404 | `/ack/{id}`: that ID isn't pending |
| **424** | **Operation failed**: login failed, MIR3 error, or at least one ack failed. Body has `"status": "failed"`, `error`, `detail` |
| 500 | The web server itself broke (bug) |

`error` values: `login_failed`, `ack_failed`, `mir3_error`, `busy`, `not_pending`.

Each ack is verified: after saving, the inbox is re-checked, and if the notification is still there the ack counts as failed.

Example:

```bash
curl -X POST http://192.168.0.104:8080/ack-all
```

```json
{
  "status": "success",
  "acked": [{"title": "ServiceNow Production Notification - INC0999032", "event_hist_id": "...", "recipient_hist_id": "...", "incident": "INC0999032"}],
  "failed": [],
  "pending_before": 1,
  "dry_run": false,
  "duration_s": 4.1
}
```

`/history` returns `{"status": "success", "count": N, "history": [...]}`, where each entry is a notification plus `timestamp`, `result` (`acked`/`failed`) and, for failures, `error`. It doesn't take the run lock, so it works while a call is running.

## Configuration (env vars)

| Var | Default | Notes |
|---|---|---|
| `MIR3_USERNAME` | — | required |
| `MIR3_PASSWORD` | — | required |
| `MIR3_BASE_URL` | — | required, e.g. `https://yourorg.mir3.com` |
| `API_PORT` | `8080` | used for both the container port and the host mapping in compose |
| `DRY_RUN` | `false` | `true` = report what would be acked, change nothing |
| `LOG_LEVEL` | `INFO` | |
| `TZ` | `America/New_York` | history timestamps |
| `MIR3_TIMEOUT_MS` | `30000` | per page action |
| `API_HOST` | `0.0.0.0` | bind inside container; leave as is |
| `DATA_DIR` | `/data` | |
| `CORS_ORIGINS` | — | comma-separated browser origins allowed to call the API, e.g. `https://mir3-ack.example.com`. Unset = no CORS headers |

## Data (`/data` volume)

- `session.json`: saved browser cookies, so restarts reuse the login. If the session expires, it logs in again automatically.
- `history.json`: log of every ack attempt (timestamp, title, incident, result, error). Keeps the last 5000. Exposed via `GET /history`.

Logs go to stdout (Portainer → container → Logs).

## Deploy on Portainer

1. Stacks → Add stack → paste `docker-compose.example.yml`, replacing `YOUR_DOCKERHUB_USER`.
2. Under **Environment variables**, add `MIR3_USERNAME`, `MIR3_PASSWORD`, `MIR3_BASE_URL`, `API_PORT` (and any optional ones).
3. Deploy. Test with `curl http://<host>:<API_PORT>/pending` before calling `/ack-all`, or set `DRY_RUN=true` first.

LAN-only: there's no auth on the API. Don't route it through the reverse proxy to the internet. To restrict the bind on the host, change the port line to `"192.168.0.104:${API_PORT}:${API_PORT}"`.

## Build locally

```bash
docker build -t mir3-acknowledgator-9000 .
docker run --rm -p 8080:8080 --env-file .env -v mir3-data:/data mir3-acknowledgator-9000
```

Without Docker:

```bash
pip install -r requirements.txt && playwright install chromium
cp .env.example .env   # fill in, then export the vars
DATA_DIR=./data python -m app.main
```

## Docker Hub publishing (GitHub Actions)

`.github/workflows/docker-publish.yml` builds and pushes on every push to `main` (`:latest`, `:sha-xxxx`) and on `v*` tags (`:1.2.3`, `:1.2`).

One-time setup:

1. **Docker Hub** → Account settings → Personal access tokens → generate a token with **Read & Write** scope.
2. **GitHub repo** → Settings → Secrets and variables → Actions:
   - **Variables** tab: `DOCKERHUB_USERNAME` = your Docker Hub username
   - **Secrets** tab: `DOCKERHUB_TOKEN` = the token
3. Push to `main`. The repo `DOCKERHUB_USERNAME/mir3-acknowledgator-9000` is created on first push (public by default on free plans).

## How it works

Uses Playwright (headless Chromium) to:

1. Load `/prepareShowActiveNotifications.do`; log in via the login form if redirected.
2. Read each `selectNotification(eventHistId, recipientHistId)` link.
3. For each one, open the Web Reply page, pick **Acknowledge**, then click **Save**.
4. Re-check the inbox to confirm it's gone.

If MIR3 changes its UI, `app/mir3.py` is the only file to update.

## License

AGPL-3.0. Derivatives, including ones run as a network service, must stay open source.
