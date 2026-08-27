import json
import os
import requests

APP_ID = os.environ["LARK_APP_ID"]
APP_SECRET = os.environ["LARK_APP_SECRET"]
BASE = os.environ.get("LARK_API_BASE", "https://open.larksuite.com/open-apis")
TIMEOUT = 15

def post(path, payload=None, token=None):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.post(f"{BASE}{path}", headers=headers, json=payload or {}, timeout=TIMEOUT)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:500]}

def get(path, token):
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{BASE}{path}", headers=headers, timeout=TIMEOUT)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:500]}

_, tenant_resp = post("/auth/v3/tenant_access_token/internal", {"app_id": APP_ID, "app_secret": APP_SECRET})
tenant = tenant_resp.get("tenant_access_token")
_, app_resp = post("/auth/v3/app_access_token/internal", {"app_id": APP_ID, "app_secret": APP_SECRET})
app_token = app_resp.get("app_access_token")
paths = [
    f"/application/v6/applications/{APP_ID}",
    f"/application/v6/applications/{APP_ID}/app_versions",
    f"/application/v6/applications/{APP_ID}/versions",
    f"/application/v6/applications/{APP_ID}/underauditlist",
    f"/application/v7/applications/{APP_ID}",
    f"/application/v7/applications/{APP_ID}/app_versions",
]
out = {"tenant_token": bool(tenant), "app_token": bool(app_token), "results": []}
for token_name, token in [("tenant", tenant), ("app", app_token)]:
    if not token:
        continue
    for path in paths:
        status, data = get(path, token)
        out["results"].append({
            "token": token_name,
            "path": path,
            "http_status": status,
            "code": data.get("code"),
            "msg": data.get("msg"),
            "data": data.get("data") if status == 200 and data.get("code") == 0 else None,
        })
print(json.dumps(out, indent=2, ensure_ascii=False))
