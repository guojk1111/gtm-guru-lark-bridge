import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests
from flask import Flask, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("gtm-guru-lark-bridge")


@dataclass(frozen=True)
class Config:
    app_id: str = os.getenv("LARK_APP_ID", "")
    app_secret: str = os.getenv("LARK_APP_SECRET", "")
    verification_token: str = os.getenv("LARK_VERIFICATION_TOKEN", "")
    encrypt_key: str = os.getenv("LARK_ENCRYPT_KEY", "")
    # Use this if your Lark event callback is configured with a custom shared token header.
    # BRIDGE_SECRET_TOKEN is accepted as an alias for deployment configs that use that name.
    inbound_bearer_token: str = os.getenv("BRIDGE_INBOUND_TOKEN") or os.getenv("BRIDGE_SECRET_TOKEN", "")
    group_id: str = os.getenv("BUYER_GTM_GROUP_ID", "oc_8a963e87591fe5023b7da9a7bfa5c9ee")
    # Lark APIs require app-scoped open_id for DMs. Username/email cannot be used directly by the send API.
    aime_user_open_id: str = os.getenv("AIME_USER_OPEN_ID", "")
    aime_user_email: str = os.getenv("AIME_USER_EMAIL", "jackson.guo@bytedance.com")
    aime_user_display_name: str = os.getenv("AIME_USER_DISPLAY_NAME", "Jackson Guo")
    lark_api_base: str = os.getenv("LARK_API_BASE", "https://open.larksuite.com/open-apis")
    sqlite_path: str = os.getenv("SQLITE_PATH", "/data/bridge_state.sqlite3")
    request_timeout: int = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
    rate_limit_window_seconds: int = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "10"))
    rate_limit_max_messages: int = int(os.getenv("RATE_LIMIT_MAX_MESSAGES", "3"))


CONFIG = Config()
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


class BridgeError(Exception):
    pass


class StateStore:
    def __init__(self, sqlite_path: str):
        self.sqlite_path = sqlite_path
        os.makedirs(os.path.dirname(sqlite_path) or ".", exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bridge_context (
                    bridge_ref TEXT PRIMARY KEY,
                    group_chat_id TEXT NOT NULL,
                    group_message_id TEXT,
                    group_thread_id TEXT,
                    original_sender_open_id TEXT,
                    original_sender_name TEXT,
                    relay_message_id TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bridge_context_status_created
                ON bridge_context(status, created_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rate_limit (
                    key TEXT NOT NULL,
                    bucket INTEGER NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY (key, bucket)
                )
                """
            )

    def save_context(self, context: Dict[str, Any]) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bridge_context (
                    bridge_ref, group_chat_id, group_message_id, group_thread_id,
                    original_sender_open_id, original_sender_name, relay_message_id,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bridge_ref) DO UPDATE SET
                    relay_message_id=excluded.relay_message_id,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    context["bridge_ref"],
                    context["group_chat_id"],
                    context.get("group_message_id"),
                    context.get("group_thread_id"),
                    context.get("original_sender_open_id"),
                    context.get("original_sender_name"),
                    context.get("relay_message_id"),
                    context.get("status", "pending"),
                    context.get("created_at", now),
                    now,
                ),
            )

    def get_by_ref(self, bridge_ref: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM bridge_context WHERE bridge_ref = ?", (bridge_ref,)
            ).fetchone()

    def get_latest_pending_for_sender(self, sender_open_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT * FROM bridge_context
                WHERE status = 'pending'
                  AND (? = '' OR original_sender_open_id IS NOT NULL)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (sender_open_id or "",),
            ).fetchone()

    def mark_completed(self, bridge_ref: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE bridge_context SET status = 'completed', updated_at = ? WHERE bridge_ref = ?",
                (int(time.time()), bridge_ref),
            )

    def check_rate_limit(self, key: str, window_seconds: int, max_messages: int) -> bool:
        bucket = int(time.time()) // window_seconds
        with self._connect() as conn:
            row = conn.execute(
                "SELECT count FROM rate_limit WHERE key = ? AND bucket = ?", (key, bucket)
            ).fetchone()
            if row and row["count"] >= max_messages:
                return False
            if row:
                conn.execute(
                    "UPDATE rate_limit SET count = count + 1 WHERE key = ? AND bucket = ?",
                    (key, bucket),
                )
            else:
                conn.execute(
                    "INSERT INTO rate_limit(key, bucket, count) VALUES (?, ?, 1)",
                    (key, bucket),
                )
        return True


store = StateStore(CONFIG.sqlite_path)


class LarkClient:
    def __init__(self, config: Config):
        self.config = config
        self._tenant_token = ""
        self._tenant_token_expire_at = 0

    def tenant_token(self) -> str:
        if self._tenant_token and time.time() < self._tenant_token_expire_at - 60:
            return self._tenant_token
        if not self.config.app_id or not self.config.app_secret:
            raise BridgeError("LARK_APP_ID and LARK_APP_SECRET must be configured")
        url = f"{self.config.lark_api_base}/auth/v3/tenant_access_token/internal"
        resp = requests.post(
            url,
            json={"app_id": self.config.app_id, "app_secret": self.config.app_secret},
            timeout=self.config.request_timeout,
        )
        data = resp.json()
        if resp.status_code >= 400 or data.get("code") != 0:
            raise BridgeError(f"Failed to get tenant token: {data}")
        self._tenant_token = data["tenant_access_token"]
        self._tenant_token_expire_at = time.time() + int(data.get("expire", 7200))
        return self._tenant_token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.tenant_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def send_text(self, receive_id_type: str, receive_id: str, text: str) -> str:
        url = f"{self.config.lark_api_base}/im/v1/messages?receive_id_type={receive_id_type}"
        payload = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        resp = requests.post(url, headers=self._headers(), json=payload, timeout=self.config.request_timeout)
        data = resp.json()
        if resp.status_code >= 400 or data.get("code") != 0:
            raise BridgeError(f"Failed to send Lark message: {data}")
        return data.get("data", {}).get("message_id", "")

    def reply_text(self, message_id: str, text: str) -> str:
        url = f"{self.config.lark_api_base}/im/v1/messages/{message_id}/reply"
        payload = {
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        resp = requests.post(url, headers=self._headers(), json=payload, timeout=self.config.request_timeout)
        data = resp.json()
        if resp.status_code >= 400 or data.get("code") != 0:
            raise BridgeError(f"Failed to reply to Lark message: {data}")
        return data.get("data", {}).get("message_id", "")


lark = LarkClient(CONFIG)


def verify_lark_request(req) -> Tuple[bool, str]:
    if CONFIG.inbound_bearer_token:
        auth_header = req.headers.get("Authorization", "")
        header_token = req.headers.get("X-Bridge-Token", "")
        query_token = req.args.get("bridge_token", "")
        expected = f"Bearer {CONFIG.inbound_bearer_token}"
        token_ok = (
            hmac.compare_digest(auth_header, expected)
            or hmac.compare_digest(header_token, CONFIG.inbound_bearer_token)
            or hmac.compare_digest(query_token, CONFIG.inbound_bearer_token)
        )
        if not token_ok:
            return False, "Invalid bridge token"

    payload = req.get_json(silent=True) or {}
    token = payload.get("token") or payload.get("header", {}).get("token")
    if CONFIG.verification_token:
        if not token or not hmac.compare_digest(token, CONFIG.verification_token):
            return False, "Invalid or missing Lark verification token"

    # Optional signature verification. Lark signature format can vary by event version.
    # If LARK_ENCRYPT_KEY is configured and timestamp/nonce/signature are present, validate them.
    signature = req.headers.get("X-Lark-Signature") or req.headers.get("X-Lark-Request-Signature")
    timestamp = req.headers.get("X-Lark-Request-Timestamp", "")
    nonce = req.headers.get("X-Lark-Request-Nonce", "")
    if CONFIG.encrypt_key and signature and timestamp and nonce:
        body = req.get_data(as_text=True)
        base = f"{timestamp}{nonce}{CONFIG.encrypt_key}{body}".encode("utf-8")
        expected_sig = hashlib.sha256(base).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            return False, "Invalid Lark signature"

    return True, "ok"


def parse_message_content(message: Dict[str, Any]) -> str:
    raw = message.get("content") or "{}"
    if isinstance(raw, dict):
        content = raw
    else:
        try:
            content = json.loads(raw)
        except json.JSONDecodeError:
            return str(raw)
    text = content.get("text") or ""
    # Remove bot mention tags like <at user_id="...">...</at> when present.
    text = re.sub(r"<at\s+user_id=\"[^\"]+\">.*?</at>", "", text).strip()
    return text


def extract_sender(event: Dict[str, Any]) -> Tuple[str, str]:
    sender = event.get("sender", {})
    sender_id = sender.get("sender_id", {})
    open_id = sender_id.get("open_id") or sender_id.get("user_id") or ""
    name = sender.get("sender_type", "user")
    # Lark event payloads often do not include display names. Preserve ID for auditability.
    return open_id, name


def extract_bridge_ref(text: str) -> Optional[str]:
    match = re.search(r"\[bridge_ref=([a-f0-9\-]{36})\]", text, re.IGNORECASE)
    return match.group(1) if match else None


def is_group_mention_event(event: Dict[str, Any]) -> bool:
    message = event.get("message", {})
    return message.get("chat_type") == "group" and message.get("chat_id") == CONFIG.group_id


def is_p2p_reply_from_aime(event: Dict[str, Any]) -> bool:
    message = event.get("message", {})
    if message.get("chat_type") != "p2p":
        return False
    sender_open_id, _ = extract_sender(event)
    return bool(CONFIG.aime_user_open_id and sender_open_id == CONFIG.aime_user_open_id)


def generate_auto_reply(text: str) -> str:
    text_lower = text.lower()
    response_lines = ["🎯 GTM GURU:\nHere is the information you might find helpful:"]
    added = False
    
    if any(k in text_lower for k in ["intake", "process", "form"]):
        response_lines.append("- Intake Form: https://bytedance.us.larkoffice.com/share/base/form/shrusXS9K6b1yLohMHQ443kekFf")
        response_lines.append("- Intake Tracker: https://bytedance.larkoffice.com/wiki/Lvz0wEuchiPnbhkW1WTcxxApnvb?table=tblvjD6z5aX5U9NU&view=vewBOKkZZp")
        response_lines.append("- Intake Process Doc: https://bytedance.larkoffice.com/wiki/Gxt6wJmWlivKhRkkM8kcxlO0nEV")
        added = True
    elif "tracker" in text_lower:
        response_lines.append("- Intake Tracker: https://bytedance.larkoffice.com/wiki/Lvz0wEuchiPnbhkW1WTcxxApnvb?table=tblvjD6z5aX5U9NU&view=vewBOKkZZp")
        added = True
        
    if "hub" in text_lower:
        response_lines.append("- GTM Hub: https://bytedance.larkoffice.com/wiki/HtfdwLhJgi3aavkr8RHcqjKmnke")
        response_lines.append("- How to Use GTM Hub: https://bytedance.larkoffice.com/wiki/OCAgwLT4Qi7vsIk14MLc9uzMnzh")
        added = True

    if not added:
        response_lines.append("- Intake Form: https://bytedance.us.larkoffice.com/share/base/form/shrusXS9K6b1yLohMHQ443kekFf")
        response_lines.append("- Intake Tracker: https://bytedance.larkoffice.com/wiki/Lvz0wEuchiPnbhkW1WTcxxApnvb?table=tblvjD6z5aX5U9NU&view=vewBOKkZZp")
        response_lines.append("- Intake Process Doc: https://bytedance.larkoffice.com/wiki/Gxt6wJmWlivKhRkkM8kcxlO0nEV")
        response_lines.append("- GTM Hub: https://bytedance.larkoffice.com/wiki/HtfdwLhJgi3aavkr8RHcqjKmnke")
        
    return "\n".join(response_lines)


def relay_group_mention_to_aime(event: Dict[str, Any]) -> Dict[str, Any]:
    message = event.get("message", {})
    sender_open_id, sender_name = extract_sender(event)
    if not store.check_rate_limit(
        sender_open_id or "unknown",
        CONFIG.rate_limit_window_seconds,
        CONFIG.rate_limit_max_messages,
    ):
        raise BridgeError("Rate limit exceeded for sender")

    text = parse_message_content(message)
    bridge_ref = str(uuid.uuid4())
    group_message_id = message.get("message_id", "")
    group_thread_id = message.get("thread_id", "")

    auto_reply_text = generate_auto_reply(text)
    if group_message_id:
        lark.reply_text(group_message_id, auto_reply_text)
    else:
        lark.send_text("chat_id", CONFIG.group_id, auto_reply_text)

    context = {
        "bridge_ref": bridge_ref,
        "group_chat_id": CONFIG.group_id,
        "group_message_id": group_message_id,
        "group_thread_id": group_thread_id,
        "original_sender_open_id": sender_open_id,
        "original_sender_name": sender_name,
        "relay_message_id": "",
        "status": "completed",
    }
    store.save_context(context)
    logger.info("Auto-replied group mention bridge_ref=%s", bridge_ref)
    return {"bridge_ref": bridge_ref, "relay_message_id": "", "auto_replied": True}


def mirror_aime_reply_to_group(event: Dict[str, Any]) -> Dict[str, Any]:
    message = event.get("message", {})
    text = parse_message_content(message)
    bridge_ref = extract_bridge_ref(text)
    row = store.get_by_ref(bridge_ref) if bridge_ref else None
    if not row:
        sender_open_id, _ = extract_sender(event)
        row = store.get_latest_pending_for_sender(sender_open_id)
    if not row:
        raise BridgeError("No pending bridge context found for Aime reply")

    clean_text = re.sub(r"\[bridge_ref=[a-f0-9\-]{36}\]", "", text, flags=re.IGNORECASE).strip()
    outbound = f"🎯 GTM GURU:\n{clean_text}"

    # Prefer replying to the original group message when available. This keeps context tighter.
    target_group_message_id = row["group_message_id"]
    if target_group_message_id:
        mirrored_message_id = lark.reply_text(target_group_message_id, outbound)
    else:
        mirrored_message_id = lark.send_text("chat_id", row["group_chat_id"], outbound)
    store.mark_completed(row["bridge_ref"])
    logger.info("Mirrored Aime reply bridge_ref=%s message_id=%s", row["bridge_ref"], mirrored_message_id)
    return {"bridge_ref": row["bridge_ref"], "mirrored_message_id": mirrored_message_id}


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "gtm-guru-lark-bridge"})


@app.post("/webhook/lark")
def lark_event_handler():
    payload = request.get_json(silent=True) or {}

    if payload.get("type") == "url_verification":
        return jsonify({"challenge": payload.get("challenge")})

    ok, reason = verify_lark_request(request)
    if not ok:
        logger.warning("Rejected Lark request: %s", reason)
        return jsonify({"code": 401, "msg": reason}), 401

    event = payload.get("event", {})
    event_type = payload.get("header", {}).get("event_type") or payload.get("type")
    if event_type and event_type != "im.message.receive_v1":
        return jsonify({"code": 0, "msg": "ignored event type"})

    try:
        if is_group_mention_event(event):
            result = relay_group_mention_to_aime(event)
            return jsonify({"code": 0, "msg": "relayed", "data": result})
        if is_p2p_reply_from_aime(event):
            result = mirror_aime_reply_to_group(event)
            return jsonify({"code": 0, "msg": "mirrored", "data": result})
        return jsonify({"code": 0, "msg": "ignored"})
    except BridgeError as exc:
        logger.exception("Bridge handling failed")
        return jsonify({"code": 500, "msg": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
