import json
import os
import sys
import requests

APP_ID = os.environ["LARK_APP_ID"]
APP_SECRET = os.environ["LARK_APP_SECRET"]
EMAIL = os.environ.get("LOOKUP_EMAIL", "jackson.guo@bytedance.com")
BASE = os.environ.get("LARK_API_BASE", "https://open.larksuite.com/open-apis")
TIMEOUT = 15


def post(path, token=None, payload=None):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.post(f"{BASE}{path}", headers=headers, json=payload or {}, timeout=TIMEOUT)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text[:500]}
    return resp.status_code, data


def get(path, token=None):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(f"{BASE}{path}", headers=headers, timeout=TIMEOUT)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text[:500]}
    return resp.status_code, data


def summarize_token(value):
    if not value:
        return None
    return {"present": True, "prefix": value[:8], "length": len(value)}

out = {"base": BASE, "app_id": APP_ID}

status, app_token_resp = post("/auth/v3/app_access_token/internal", payload={"app_id": APP_ID, "app_secret": APP_SECRET})
out["app_access_token"] = {
    "http_status": status,
    "code": app_token_resp.get("code"),
    "msg": app_token_resp.get("msg"),
    "token_summary": summarize_token(app_token_resp.get("app_access_token")),
    "expire": app_token_resp.get("expire"),
}

status, tenant_token_resp = post("/auth/v3/tenant_access_token/internal", payload={"app_id": APP_ID, "app_secret": APP_SECRET})
tenant_token = tenant_token_resp.get("tenant_access_token")
out["tenant_access_token"] = {
    "http_status": status,
    "code": tenant_token_resp.get("code"),
    "msg": tenant_token_resp.get("msg"),
    "token_summary": summarize_token(tenant_token),
    "expire": tenant_token_resp.get("expire"),
}

if tenant_token:
    # Primary lookup by verified email. This normally requires contact:user.id:readonly.
    status, lookup_resp = post(
        "/contact/v3/users/batch_get_id?user_id_type=open_id",
        token=tenant_token,
        payload={"emails": [EMAIL]},
    )
    out["user_lookup_by_email"] = {
        "http_status": status,
        "code": lookup_resp.get("code"),
        "msg": lookup_resp.get("msg"),
        "data": lookup_resp.get("data"),
    }
else:
    out["user_lookup_by_email"] = {"skipped": "tenant token unavailable"}

print(json.dumps(out, indent=2, ensure_ascii=False))
