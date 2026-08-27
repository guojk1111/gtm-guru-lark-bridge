# GTM GURU Lark Bridge Bot

A lightweight Flask webhook service that bridges Lark group @mentions to GTM GURU/Aime and mirrors the reply back to the Buyer GTM Intake Group.

## What it does

1. Receives Lark `im.message.receive_v1` callbacks at `POST /webhook/lark`.
2. Handles Lark URL verification by echoing the challenge.
3. Validates incoming requests with the Lark verification token and optional bridge bearer token.
4. Detects @mention messages from the Buyer GTM Intake Group.
5. Sends the message as a P2P DM to Jackson/GTM GURU with a `[bridge_ref=...]` correlation token.
6. Receives Jackson/GTM GURU P2P replies and mirrors them back to the originating group message, prefixed with `🎯 GTM GURU:`.
7. Stores correlation state in SQLite so the service can survive restarts when `/data` is mounted to persistent storage.

## Required Lark app configuration

Create an internal enterprise app named **GTM GURU Bot** and enable Bot capability.

Required scopes:

- `im:message.group_at_msg:readonly` — receive group @mention messages.
- `im:message:send_as_bot` — send messages as the bot.
- `im:chat` or the current platform equivalent for chat read access.
- `im:message.p2p_msg:readonly` — required to receive P2P replies from Jackson/GTM GURU. This is included in the implementation plan even though it was not in the short scope list.

Event subscription:

- Event: `im.message.receive_v1`
- Request URL: `https://<your-host>/webhook/lark`

Group:

- Buyer GTM Intake Group: `oc_8a963e87591fe5023b7da9a7bfa5c9ee`

## Environment variables

| Variable | Required | Example | Notes |
| --- | --- | --- | --- |
| `LARK_APP_ID` | Yes | `cli_xxx` | App ID from Lark Open Platform. |
| `LARK_APP_SECRET` | Yes | `***` | App secret. Do not commit it. |
| `LARK_VERIFICATION_TOKEN` | Recommended | `***` | Event subscription verification token. |
| `LARK_ENCRYPT_KEY` | Optional | `***` | Enables signature verification if Lark sends compatible headers. |
| `BRIDGE_INBOUND_TOKEN` | Optional | `***` | Requires `Authorization: Bearer <token>` on callback requests if set. Usually only use this behind a gateway that can inject the header. |
| `BUYER_GTM_GROUP_ID` | Yes | `oc_8a963e87591fe5023b7da9a7bfa5c9ee` | Defaults to the Buyer GTM Intake Group ID. |
| `AIME_USER_OPEN_ID` | Yes | `ou_xxx` | App-scoped open_id for Jackson/GTM GURU. Username/email cannot be used directly by Lark message send API. |
| `AIME_USER_EMAIL` | No | `jackson.guo@bytedance.com` | For operator reference. |
| `AIME_USER_DISPLAY_NAME` | No | `Jackson Guo` | For operator reference. |
| `SQLITE_PATH` | No | `/data/bridge_state.sqlite3` | Persist this path in production. |
| `PORT` | No | `8080` | Server port. |

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export LARK_APP_ID="cli_xxx"
export LARK_APP_SECRET="***"
export LARK_VERIFICATION_TOKEN="***"
export BUYER_GTM_GROUP_ID="oc_8a963e87591fe5023b7da9a7bfa5c9ee"
export AIME_USER_OPEN_ID="ou_xxx"
python app.py
```

Health check:

```bash
curl http://localhost:8080/healthz
```

URL verification test:

```bash
curl -X POST http://localhost:8080/webhook/lark \
  -H 'Content-Type: application/json' \
  -d '{"type":"url_verification","challenge":"test_challenge"}'
```

## Docker

```bash
docker build -t gtm-guru-lark-bridge .
docker run --rm -p 8080:8080 \
  -e LARK_APP_ID="cli_xxx" \
  -e LARK_APP_SECRET="***" \
  -e LARK_VERIFICATION_TOKEN="***" \
  -e BUYER_GTM_GROUP_ID="oc_8a963e87591fe5023b7da9a7bfa5c9ee" \
  -e AIME_USER_OPEN_ID="ou_xxx" \
  -v gtm-guru-bridge-data:/data \
  gtm-guru-lark-bridge
```

## Manual production steps still required

1. Create the app in Lark Open Platform with Jackson Guo's enterprise access.
2. Enable the bot capability and release/publish the internal app if Lark requires version approval.
3. Add the required scopes and get admin approval if prompted.
4. Deploy this service to an HTTPS host reachable by Lark.
5. Configure the event callback URL to `https://<your-host>/webhook/lark`.
6. Add **GTM GURU Bot** to the Buyer GTM Intake Group.
7. Resolve and configure Jackson's app-scoped `AIME_USER_OPEN_ID` for this new app.
8. Test the end-to-end path: group @mention → Jackson/GTM GURU DM → reply with `[bridge_ref=...]` → group mirror.

## Operational notes

- Keep `[bridge_ref=...]` in the DM reply for deterministic routing. If it is missing, the service falls back to the latest pending context.
- For production hardening, run behind a reverse proxy or cloud function gateway with TLS, access logs, and retry protection.
- The service intentionally does not print app secrets, access tokens, or credentials in logs.

