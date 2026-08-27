import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import google.generativeai as genai
import requests
from flask import Flask, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("gtm-guru-lark-bridge")


GTM_GURU_SYSTEM_PROMPT = """You are GTM GURU, the AI assistant for the US Buyer Customer Service Operations GTM process at ByteDance/TikTok Shop. You are answering questions posted in the Buyer GTM Intake Group on Lark.

Your knowledge base:
- GTM Hub (single source of truth): https://bytedance.larkoffice.com/wiki/HtfdwLhJgi3aavkr8RHcqjKmnke
- How to Use GTM Hub: https://bytedance.larkoffice.com/wiki/OCAgwLT4Qi7vsIk14MLc9uzMnzh
- GTM Intake Form (submit new requests): https://bytedance.us.larkoffice.com/share/base/form/shrusXS9K6b1yLohMHQ443kekFf
- GTM Intake Tracker (all active projects): https://bytedance.larkoffice.com/wiki/Lvz0wEuchiPnbhkW1WTcxxApnvb?table=tblvjD6z5aX5U9NU&view=vewBOKkZZp
- Intake Process Doc: https://bytedance.larkoffice.com/wiki/Gxt6wJmWlivKhRkkM8kcxlO0nEV
- One-pager template: https://bytedance.us.larkoffice.com/docx/BPy2d2av9oQSegxo5Hsuy5uXssh
- Reference doc: https://bytedance.larkoffice.com/docx/MW8ydmjomowzaJxQpeCcf5m2nff

Key context:
- Owner: Jackson Guo (Operations, Seattle)
- Key teams: SOP team, QA team, OPS team
- Key stakeholders: Kevin Cabrera, Diana Ornstein, Dazhi Yu
- Q3 2026 focus: US Buyer GTM process buildout — lock current state, build shared space, run parallel routing via GTM template + AI agent
- QA must be involved from the START of any change, not after go-live
- Upstream GNE/Business Ops coordination is OUT OF SCOPE for Q3 (planned Q4)
- Intake types: Top Down Project (routed to @jenniferwang) | All other types (routed to @kevin.cabrera)

Answer style:
- Be concise and direct — 3-5 sentences max unless a detailed breakdown is needed
- Always include the most relevant link(s) from the knowledge base
- If you don't know the specific answer, point to the GTM Intake Tracker or GTM Hub
- Never make up project statuses or data — say "check the tracker" if unsure
"""

GTM_LINKS = {
    "hub": "🏠 GTM Hub: https://bytedance.larkoffice.com/wiki/HtfdwLhJgi3aavkr8RHcqjKmnke",
    "how_to": "📖 How to Use GTM Hub: https://bytedance.larkoffice.com/wiki/OCAgwLT4Qi7vsIk14MLc9uzMnzh",
    "form": "📋 Intake Form: https://bytedance.us.larkoffice.com/share/base/form/shrusXS9K6b1yLohMHQ443kekFf",
    "tracker": "🔍 Intake Tracker: https://bytedance.larkoffice.com/wiki/Lvz0wEuchiPnbhkW1WTcxxApnvb?table=tblvjD6z5aX5U9NU&view=vewBOKkZZp",
    "process": "📖 Intake Process Doc: https://bytedance.larkoffice.com/wiki/Gxt6wJmWlivKhRkkM8kcxlO0nEV",
    "template": "📄 One-pager template: https://bytedance.us.larkoffice.com/docx/BPy2d2av9oQSegxo5Hsuy5uXssh",
}

INTENT_RULES = [
    {
        "name": "intake",
        "keywords": [
            "submit",
            "new request",
            "intake",
            "kick off",
            "kickoff",
            "start a project",
            "raise",
            "file",
            "how do i submit",
            "how to submit",
            "new change",
        ],
        "response": (
            "To submit a new GTM request, fill out the Intake Form — it takes about 5 minutes and auto-routes to the right team. "
            "Top Down Projects go to @jenniferwang, and all other intake types go to @kevin.cabrera. "
            "Once it is submitted, you can track progress live in the Intake Tracker."
        ),
        "links": ["form", "tracker"],
    },
    {
        "name": "tracker",
        "keywords": [
            "tracker",
            "status",
            "where is",
            "my request",
            "project status",
            "update",
            "progress",
            "what's the status",
            "whats the status",
        ],
        "response": (
            "Check the GTM Intake Tracker for live project status — it shows the owner, due date, and current stage for active requests. "
            "If your project is not listed there yet, it likely has not been submitted through the Intake Form."
        ),
        "links": ["tracker", "form"],
    },
    {
        "name": "process",
        "keywords": [
            "process",
            "how does",
            "how do",
            "steps",
            "workflow",
            "procedure",
            "what happens",
            "timeline",
            "lead time",
            "how long",
        ],
        "response": (
            "The GTM process runs in parallel tracks, so SOP, Ops/Training, and QA are looped in from day one instead of moving sequentially. "
            "QA must be involved from the start, not after go-live. "
            "The Intake Process Doc has the full workflow and standard lead-time guidance."
        ),
        "links": ["process", "hub"],
    },
    {
        "name": "qa",
        "keywords": ["qa", "quality", "test", "testing", "sign off", "sign-off", "approval"],
        "response": (
            "QA must be looped in at the start of every change — not after go-live. "
            "That is a hard rule in the GTM process. "
            "For QA sign-off expectations and checkpoints, use the Intake Process Doc."
        ),
        "links": ["process", "hub"],
    },
    {
        "name": "routing",
        "keywords": [
            "who",
            "owner",
            "route",
            "routing",
            "tag",
            "assign",
            "responsible",
            "kevin",
            "jennifer",
            "diana",
            "dazhi",
        ],
        "response": (
            "Routing depends on project type: Top Down Projects should tag @jenniferwang, and all other intake types should tag @kevin.cabrera. "
            "For downstream coordination, Diana Ornstein supports OPS and Dazhi Yu supports SOP and QA. "
            "The Intake Process Doc has the full routing logic."
        ),
        "links": ["process"],
    },
    {
        "name": "hub",
        "keywords": ["hub", "where", "find", "resource", "link", "doc", "document", "wiki", "one-pager", "template"],
        "response": (
            "The GTM Hub is the single source of truth for US Buyer GTM resources. "
            "Process docs, trackers, templates, and one-pagers all live there, so it is the best starting point when you are not sure where to look."
        ),
        "links": ["hub", "how_to"],
    },
    {
        "name": "scope",
        "keywords": ["scope", "q3", "q4", "quarter", "in scope", "out of scope", "gne", "business ops", "upstream", "expansion"],
        "response": (
            "Q3 2026 is focused on locking the current state, building the shared GTM space, and running the manual GTM flow. "
            "Upstream expansion to GNE and Business Ops is out of scope for Q3 and planned for Q4 2026."
        ),
        "links": ["hub"],
    },
]


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
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
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
                CREATE TABLE IF NOT EXISTS rate_limit (
                    key TEXT NOT NULL,
                    bucket INTEGER NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY (key, bucket)
                )
                """
            )
            # Deduplication table: stores processed event_ids to prevent duplicate replies
            # when Lark retries delivery or multiple gunicorn workers race.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_events (
                    event_id TEXT PRIMARY KEY,
                    processed_at INTEGER NOT NULL
                )
                """
            )

    def is_event_processed(self, event_id: str) -> bool:
        """Returns True if this event_id was already processed (dedup check). Atomically marks it processed."""
        now = int(time.time())
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO processed_events (event_id, processed_at) VALUES (?, ?)",
                    (event_id, now),
                )
                # Clean up old events older than 1 hour to keep the table small
                conn.execute(
                    "DELETE FROM processed_events WHERE processed_at < ?",
                    (now - 3600,),
                )
                return False  # Not a duplicate — we just inserted it
            except sqlite3.IntegrityError:
                return True  # Duplicate — already processed

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
    # Check bridge token (query param or header)
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

    # Lark verification token check — token may appear in payload root OR in header object.
    # For Lark event callback v2, the token is in payload["header"]["token"].
    # For older v1 format, it's in payload["token"].
    # If no token is found in payload, we skip this check to avoid false rejections.
    if CONFIG.verification_token:
        payload = req.get_json(silent=True) or {}
        token = payload.get("token") or payload.get("header", {}).get("token")
        if token:
            if not hmac.compare_digest(token, CONFIG.verification_token):
                return False, "Invalid Lark verification token"
        # If token is absent from payload, allow through (Lark may omit it for some event types)

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


def is_group_mention_event(event: Dict[str, Any]) -> bool:
    message = event.get("message", {})
    return message.get("chat_type") == "group" and message.get("chat_id") == CONFIG.group_id


def normalize_question(question: str) -> str:
    return re.sub(r"\s+", " ", question.lower()).strip()


def count_keyword_matches(normalized_question: str, keywords: list[str]) -> int:
    return sum(1 for keyword in keywords if keyword in normalized_question)


def build_auto_reply(response_text: str, link_keys: list[str]) -> str:
    unique_links = []
    for link_key in link_keys:
        link_value = GTM_LINKS[link_key]
        if link_value not in unique_links:
            unique_links.append(link_value)
    return response_text + "\n" + "\n".join(unique_links[:2])


def generate_auto_reply(question: str) -> str:
    normalized = normalize_question(question)
    best_rule = None
    best_score = 0

    for rule in INTENT_RULES:
        score = count_keyword_matches(normalized, rule["keywords"])
        if score > best_score:
            best_rule = rule
            best_score = score

    if best_rule:
        return build_auto_reply(best_rule["response"], best_rule["links"])

    default_response = (
        "I'm GTM GURU — here to help with the US Buyer GTM process. "
        "Check the GTM Hub for process docs, the Intake Tracker for live project status, or submit a new request through the Intake Form."
    )
    return build_auto_reply(default_response, ["hub", "tracker", "form"])


def generate_gemini_answer(question: str) -> str:
    if not CONFIG.gemini_api_key:
        logger.info("GEMINI_API_KEY is not configured; using smart auto reply")
        return generate_auto_reply(question)

    try:
        genai.configure(api_key=CONFIG.gemini_api_key)
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            system_instruction=GTM_GURU_SYSTEM_PROMPT,
        )
        response = model.generate_content(question)
        answer = (getattr(response, "text", "") or "").strip()
        if not answer:
            raise BridgeError("Gemini returned an empty response")
        return answer
    except Exception:
        logger.exception("Gemini call failed; using fallback response")
        return fallback_answer(question)


def answer_group_mention_with_gemini(event: Dict[str, Any]) -> Dict[str, Any]:
    message = event.get("message", {})
    sender_open_id, _ = extract_sender(event)
    if not store.check_rate_limit(
        sender_open_id or "unknown",
        CONFIG.rate_limit_window_seconds,
        CONFIG.rate_limit_max_messages,
    ):
        raise BridgeError("Rate limit exceeded for sender")

    question = parse_message_content(message)
    answer = generate_gemini_answer(question)
    outbound = f"🎯 GTM GURU:\n{answer}"
    group_message_id = message.get("message_id", "")
    if not group_message_id:
        raise BridgeError("Missing group message_id for reply")

    reply_message_id = lark.reply_text(group_message_id, outbound)
    logger.info("Answered group mention with Gemini reply_message_id=%s", reply_message_id)
    return {"reply_message_id": reply_message_id, "answered": True}


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "gtm-guru-lark-bridge", "build": "v7-smart-fallback"})


@app.get("/version")
def get_version():
    import os
    import subprocess
    commit_hash = os.getenv("RENDER_GIT_COMMIT")
    if not commit_hash:
        try:
            commit_hash = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL).decode("utf-8").strip()
        except Exception:
            commit_hash = "unknown"
    return jsonify({"ok": True, "service": "gtm-guru-lark-bridge", "version": commit_hash})


@app.post("/webhook/lark")
def lark_event_handler():
    payload = request.get_json(silent=True) or {}

    if payload.get("type") == "url_verification":
        return jsonify({"challenge": payload.get("challenge")})

    ok, reason = verify_lark_request(request)
    if not ok:
        logger.warning("Rejected Lark request: %s", reason)
        return jsonify({"code": 401, "msg": reason}), 401

    # --- Deduplication: Lark retries unacknowledged events up to 3x ---
    # Use event_id from header to ensure we only process each event once.
    header = payload.get("header", {})
    event_id = header.get("event_id") or payload.get("event_id", "")
    if event_id and store.is_event_processed(event_id):
        logger.info("Duplicate event ignored event_id=%s", event_id)
        return jsonify({"code": 0, "msg": "duplicate ignored"})

    event = payload.get("event", {})
    event_type = header.get("event_type") or payload.get("type")
    if event_type and event_type != "im.message.receive_v1":
        return jsonify({"code": 0, "msg": "ignored event type"})

    try:
        if is_group_mention_event(event):
            result = answer_group_mention_with_gemini(event)
            return jsonify({"code": 0, "msg": "answered", "data": result})
        return jsonify({"code": 0, "msg": "ignored"})
    except BridgeError as exc:
        logger.exception("Bridge handling failed")
        return jsonify({"code": 500, "msg": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
