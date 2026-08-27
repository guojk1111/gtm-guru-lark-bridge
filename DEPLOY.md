# GTM GURU Bridge — Permanent Hosting (Render / Railway / Fly)

The bridge server is fully self-contained and runs on any Python 3.11+ host that can serve HTTPS on `$PORT`. Signup + repo-push requires a human (email/phone verification, OAuth), so this guide covers what Jackson needs to click, and everything is pre-wired so it deploys in ~3 minutes.

## Option A — Render.com (recommended, 3 min)

1. Sign up at https://dashboard.render.com/register (use `jackson.guo@bytedance.com` or a fresh Gmail; phone verification required).
2. Push this folder to a GitHub repo:
   ```bash
   cd gtm-guru-lark-bridge
   git remote add origin git@github.com:<your-gh>/gtm-guru-lark-bridge.git
   git push -u origin main
   ```
3. In Render: **New +** → **Blueprint** → connect the repo. Render will read `render.yaml` and auto-provision the service.
4. Set the three secret env vars (Render will prompt because `sync: false`):
   - `LARK_APP_SECRET` = `rC1MnbHXBV5VCEAixPU0fhdjWV8KtH5g`
   - `LARK_VERIFICATION_TOKEN` = `TvFW8GWBUyCLShZGYPw3rwF8KYUkY0Kk`
   - `BRIDGE_SECRET_TOKEN` = `gtmguru2026`
5. Wait ~2 min for build. Copy the service URL (e.g. `https://gtm-guru-lark-bridge.onrender.com`).
6. Sanity check:
   ```bash
   curl https://gtm-guru-lark-bridge.onrender.com/healthz
   ```
7. Paste this into Lark Event Subscriptions Request URL:
   ```
   https://gtm-guru-lark-bridge.onrender.com/webhook/lark?bridge_token=gtmguru2026
   ```

> ⚠️ Free plan sleeps after 15 min idle. First request after sleep takes ~30s. For always-on, upgrade to Starter ($7/mo) or use Railway.

## Option B — Railway.app (also ~3 min, no sleep on free trial)

1. Sign up at https://railway.app/ with GitHub.
2. **New Project → Deploy from GitHub repo** → pick this repo.
3. Railway auto-detects the Dockerfile. Add the env vars (same list as above).
4. Enable **Public Networking** → get a `*.up.railway.app` URL.
5. Paste that URL + `/webhook/lark?bridge_token=gtmguru2026` into Lark.

## Option C — Fly.io (Docker, generous free tier)

```bash
cd gtm-guru-lark-bridge
fly launch --no-deploy   # accept defaults
fly secrets set LARK_APP_ID=YOUR_APP_ID \
                LARK_APP_SECRET=YOUR_APP_SECRET \
                LARK_VERIFICATION_TOKEN=YOUR_VERIFICATION_TOKEN \
                AIME_USER_OPEN_ID=ou_82ca1e7acc83296b84930b6dd39951da \
                BUYER_GTM_GROUP_ID=oc_8a963e87591fe5023b7da9a7bfa5c9ee \
                BRIDGE_SECRET_TOKEN=gtmguru2026
fly deploy
```

## Post-deploy checklist

- [ ] `GET /healthz` returns `{"ok":true}`
- [ ] `POST /webhook/lark?bridge_token=gtmguru2026` with `{"type":"url_verification","challenge":"abc","token":"..."}` returns `{"challenge":"abc"}`
- [ ] Paste URL into Lark Developer Console → Event Subscriptions → Request URL → **Save**
- [ ] Real @mention in Buyer GTM Intake Group → Jackson gets DM
- [ ] Reply to DM → group receives `🎯 GTM GURU: …` mirror
