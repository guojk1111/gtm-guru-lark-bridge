import json
import os
import requests

APP_ID = os.environ["LARK_APP_ID"]
APP_SECRET = os.environ["LARK_APP_SECRET"]
GROUP_ID = os.environ.get("GROUP_ID", "oc_8a963e87591fe5023b7da9a7bfa5c9ee")
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

def get(path, token=None):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.get(f"{BASE}{path}", headers=headers, timeout=TIMEOUT)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:500]}

_, token_resp = post("/auth/v3/tenant_access_token/internal", {"app_id": APP_ID, "app_secret": APP_SECRET})
token = token_resp.get("tenant_access_token")
out = {"tenant_token_ok": bool(token), "checks": []}
if token:
    checks = [
        ("im:chat read / bot group membership check", "GET", f"/im/v1/chats/{GROUP_ID}"),
        ("bot info check", "GET", "/bot/v3/info"),
    ]
    for name, method, path in checks:
        status, data = get(path, token=token)
        out["checks"].append({
            "name": name,
            "http_status": status,
            "code": data.get("code"),
            "msg": data.get("msg"),
            "data_keys": list((data.get("data") or {}).keys()) if isinstance(data.get("data"), dict) else None,
        })
print(json.dumps(out, indent=2, ensure_ascii=False))
