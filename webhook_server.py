import logging
import os
import hmac
import hashlib
import json
import re
import threading
import requests
from datetime import datetime, timedelta
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv

from main import run_agent

load_dotenv()
log = logging.getLogger(__name__)
os.makedirs("logs", exist_ok=True)

# In-memory anti-duplicate reply cache: {sender_number: last_reply_utc}
_LAST_GREETING_REPLY_AT: dict[str, datetime] = {}
_PROCESSED_MESSAGE_IDS: dict[str, datetime] = {}
_LAST_WEEKLY_REPORT_REPLY_AT: dict[str, datetime] = {}
_PDF_GENERATION_CONDITION = threading.Condition()
_PDF_GENERATION_IN_PROGRESS = False


def _latest_pdf_path() -> Path | None:
    out_dir = Path("output")
    if not out_dir.exists():
        return None
    pdfs = sorted(out_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    return pdfs[0] if pdfs else None


def _is_older_than(path: Path, max_age_minutes: int) -> bool:
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return datetime.now() - mtime > timedelta(minutes=max_age_minutes)


def _get_or_generate_pdf() -> Path:
    max_age_minutes = int(os.getenv("WEBHOOK_PDF_MAX_AGE_MINUTES", "60"))
    latest = _latest_pdf_path()
    if latest and not _is_older_than(latest, max_age_minutes):
        return latest
    with _PDF_GENERATION_CONDITION:
        latest = _latest_pdf_path()
        if latest and not _is_older_than(latest, max_age_minutes):
            return latest

        global _PDF_GENERATION_IN_PROGRESS
        if _PDF_GENERATION_IN_PROGRESS:
            log.info("PDF generation already in progress; waiting for completion")
            while _PDF_GENERATION_IN_PROGRESS:
                _PDF_GENERATION_CONDITION.wait()
            latest = _latest_pdf_path()
            if latest and not _is_older_than(latest, max_age_minutes):
                return latest

        _PDF_GENERATION_IN_PROGRESS = True

    try:
        generated_path = run_agent(distribute=False)
        return Path(generated_path)
    finally:
        with _PDF_GENERATION_CONDITION:
            _PDF_GENERATION_IN_PROGRESS = False
            _PDF_GENERATION_CONDITION.notify_all()


class WebhookHandler(BaseHTTPRequestHandler):
    def _is_authorized_non_meta_get(self, parsed) -> bool:
        """
        Auth gate for non-Meta GET requests (e.g. PDF fetch).
        Accepts either:
          - X-Webhook-Auth: <token>
          - ?auth_token=<token>
        """
        expected = self._env_clean("WEBHOOK_AUTH_TOKEN")
        if not expected:
            return False
        query = parse_qs(parsed.query or "")
        q_token = (query.get("auth_token") or [""])[0]
        h_token = (self.headers.get("X-Webhook-Auth") or "").strip()
        return bool((q_token and q_token == expected) or (h_token and h_token == expected))

    def _parse_whitelist_numbers(self) -> set[str]:
        """
        Parse WHATSAPP_TO_NUMBERS from either JSON array or CSV string.
        """
        raw = self._env_clean("WHATSAPP_TO_NUMBERS", "")
        if not raw:
            return set()
        nums: list[str] = []
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    nums = [str(x).strip() for x in parsed if str(x).strip()]
            except Exception:
                nums = []
        if not nums:
            nums = [x.strip() for x in raw.split(",") if x.strip()]
        return set(nums)

    def _env_clean(self, key: str, default: str = "") -> str:
        v = (os.getenv(key) or default).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1].strip()
        return v

    def _is_greeting(self, text: str) -> bool:
        cleaned = (text or "").strip().lower()
        cleaned = cleaned.replace("!", "").replace(".", "").replace(",", "")
        return cleaned in {"hi", "hello", "hey", "hii", "heyy"}

    def _should_reply_now(self, sender: str, dedupe_seconds: int = 30) -> bool:
        now = datetime.utcnow()
        last = _LAST_GREETING_REPLY_AT.get(sender)
        if last and (now - last).total_seconds() < dedupe_seconds:
            return False
        _LAST_GREETING_REPLY_AT[sender] = now
        return True
    def _is_stale_message(self, timestamp: str, max_age_seconds: int = 180) -> bool:
        try:
            if not timestamp:
                log.warning("Missing timestamp → treating as stale")
                return True

            msg_time = datetime.utcfromtimestamp(int(timestamp))
            log.warning("Message timestamp: %s", msg_time)

            now = datetime.utcnow()
            log.warning("Current time: %s", now)

            delay = (now - msg_time).total_seconds()
            log.warning("Delay: %s seconds", delay)

            if delay > max_age_seconds:
                log.warning("Delay > max_age (%s), marking stale", max_age_seconds)
                return True

        except Exception as e:
            log.error("Timestamp parsing failed: %s (timestamp=%s)", e, timestamp)
            return True

        return False
    def _is_duplicate_message_id(self, message_id: str | None, ttl_seconds: int = 600) -> bool:
        """Return True if message id already seen recently; also prunes stale entries."""
        now = datetime.utcnow()
        stale_before = now - timedelta(seconds=ttl_seconds)
        for mid, ts in list(_PROCESSED_MESSAGE_IDS.items()):
            if ts < stale_before:
                _PROCESSED_MESSAGE_IDS.pop(mid, None)
        if not message_id:
            return False
        if message_id in _PROCESSED_MESSAGE_IDS:
            return True
        _PROCESSED_MESSAGE_IDS[message_id] = now
        return False

    def _should_send_weekly_report(self, sender: str, cooldown_seconds: int = 120) -> bool:
        now = datetime.utcnow()
        last = _LAST_WEEKLY_REPORT_REPLY_AT.get(sender)
        if last and (now - last).total_seconds() < cooldown_seconds:
            return False
        _LAST_WEEKLY_REPORT_REPLY_AT[sender] = now
        return True

    def _append_webhook_audit(self, payload: dict):
        """Persist incoming webhook payloads for troubleshooting/audit."""
        try:
            audit_path = Path("logs") / "webhook_events.jsonl"
            with audit_path.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "received_at": datetime.now().isoformat(),
                            "payload": payload,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception as e:
            log.warning("Failed to persist webhook payload: %s", e)

    def _extract_text_messages(self, payload: dict) -> list[str]:
        """
        Extract WhatsApp incoming text message bodies from webhook payload.
        """
        texts: list[str] = []
        try:
            for entry in payload.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {}) or {}
                    for msg in value.get("messages", []):
                        text_obj = msg.get("text") or {}
                        body = text_obj.get("body")
                        if body:
                            texts.append(str(body))
        except Exception:
            return texts
        return texts

    def _extract_incoming_text_messages(self, payload: dict) -> list[dict]:
        """
        Extract sender + body + message id for incoming user messages.
        Supports text messages and button replies.
        Returns: [{"from": "...", "body": "...", "id": "..."}]
        """
        out: list[dict] = []
        try:
            for entry in payload.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {}) or {}
                    for msg in value.get("messages", []):
                        sender = msg.get("from")
                        msg_id = msg.get("id")
                        msg_type = str(msg.get("type") or "").lower()
                        body = ""

                        if msg_type == "text":
                            text_obj = msg.get("text") or {}
                            body = str(text_obj.get("body") or "").strip()
                        elif msg_type == "button":
                            button_obj = msg.get("button") or {}
                            # Prefer payload because it is stable; fallback to visible button text.
                            body = str(
                                button_obj.get("payload")
                                or button_obj.get("text")
                                or ""
                            ).strip()

                        if body and sender:
                            out.append(
                                {
                                    "from": str(sender),
                                    "body": body,
                                    "id": str(msg_id) if msg_id else "",
                                    "timestamp": str(msg.get("timestamp", "")),
                                }
                            )
        except Exception:
            return out
        return out

    def _send_whatsapp_text(self, to_number: str, message_text: str) -> bool:
        api_url = self._env_clean("WHATSAPP_API_URL")
        token = self._env_clean("WHATSAPP_API_TOKEN")
        if not api_url or not token:
            log.warning("WHATSAPP_API_URL/WHATSAPP_API_TOKEN missing; cannot send reply.")
            return False
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "text",
            "text": {"body": message_text},
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            r = requests.post(api_url, headers=headers, data=json.dumps(payload), timeout=20)
            if r.status_code >= 400:
                log.error(
                    "WhatsApp reply failed status=%s body=%s",
                    r.status_code,
                    r.text[:1000],
                )
                return False
            log.info("WhatsApp reply sent status=%s body=%s", r.status_code, r.text[:500])
            return True
        except Exception as e:
            log.error("Failed to send WhatsApp reply to %s: %s", to_number, e)
            return False

    def _send_whatsapp_template(
        self,
        to_number: str,
        template_name: str,
        language_code: str = "en",
    ) -> bool:
        api_url = self._env_clean("WHATSAPP_API_URL")
        token = self._env_clean("WHATSAPP_API_TOKEN")
        if not api_url or not token:
            log.warning("WHATSAPP_API_URL/WHATSAPP_API_TOKEN missing; cannot send template.")
            return False
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
            },
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            r = requests.post(api_url, headers=headers, data=json.dumps(payload), timeout=20)
            if r.status_code >= 400:
                log.error(
                    "WhatsApp template send failed status=%s body=%s",
                    r.status_code,
                    r.text[:1000],
                )
                return False
            log.info("WhatsApp template sent status=%s body=%s", r.status_code, r.text[:500])
            return True
        except Exception as e:
            log.error("Failed to send WhatsApp template to %s: %s", to_number, e)
            return False

    def _resolve_media_upload_url(self, api_url: str) -> str:
        media_url = self._env_clean("WHATSAPP_MEDIA_UPLOAD_URL")
        if media_url:
            return media_url
        u = api_url.rstrip("/")
        if u.endswith("/messages"):
            return u[: -len("/messages")] + "/media"
        return ""

    def _upload_pdf_to_whatsapp_media(self, pdf_path: Path) -> str | None:
        api_url = self._env_clean("WHATSAPP_API_URL")
        token = self._env_clean("WHATSAPP_API_TOKEN")
        media_upload_url = self._resolve_media_upload_url(api_url)
        if not token or not media_upload_url:
            log.error("Missing token/media upload URL for WhatsApp media upload")
            return None
        try:
            with pdf_path.open("rb") as f:
                files = {"file": (pdf_path.name, f, "application/pdf")}
                data = {"messaging_product": "whatsapp", "type": "application/pdf"}
                headers = {"Authorization": f"Bearer {token}"}
                r = requests.post(media_upload_url, headers=headers, data=data, files=files, timeout=60)
            if r.status_code >= 400:
                log.error("WhatsApp media upload failed status=%s body=%s", r.status_code, r.text[:1000])
                return None
            media_id = (r.json() or {}).get("id")
            if not media_id:
                log.error("WhatsApp media upload response missing id: %s", r.text[:1000])
                return None
            return str(media_id)
        except Exception as e:
            log.error("WhatsApp media upload exception: %s", e)
            return None

    def _send_whatsapp_document(self, to_number: str, pdf_path: Path) -> bool:
        api_url = self._env_clean("WHATSAPP_API_URL")
        token = self._env_clean("WHATSAPP_API_TOKEN")
        if not api_url or not token:
            log.warning("WHATSAPP_API_URL/WHATSAPP_API_TOKEN missing; cannot send report.")
            return False
        media_id = self._upload_pdf_to_whatsapp_media(pdf_path)
        if not media_id:
            return False
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "document",
            "document": {
                "id": media_id,
                "filename": pdf_path.name,
                "caption": "Weekly report",
            },
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            r = requests.post(api_url, headers=headers, data=json.dumps(payload), timeout=20)
            if r.status_code >= 400:
                log.error("WhatsApp report send failed status=%s body=%s", r.status_code, r.text[:1000])
                return False
            log.info("WhatsApp report sent status=%s body=%s", r.status_code, r.text[:500])
            return True
        except Exception as e:
            log.error("Failed to send WhatsApp report to %s: %s", to_number, e)
            return False

    def _verify_meta_signature(self, raw_body: bytes) -> bool:
        """
        Validate X-Hub-Signature-256 when WHATSAPP_APP_SECRET is configured.
        Strict mode is controlled by WEBHOOK_STRICT_SIGNATURE:
          - true  -> reject if app secret/header/signature is invalid
          - false -> allow request but log validation warnings
        """
        strict = self._env_clean("WEBHOOK_STRICT_SIGNATURE", "false").lower() in {"1", "true", "yes", "on"}
        app_secret = self._env_clean("WHATSAPP_APP_SECRET")
        if not app_secret:
            msg = "WHATSAPP_APP_SECRET missing"
            if strict:
                log.error("%s; rejecting POST webhook", msg)
                return False
            log.warning("%s; allowing POST because WEBHOOK_STRICT_SIGNATURE=false", msg)
            return True
        signature_header = self.headers.get("X-Hub-Signature-256", "")
        if not signature_header.startswith("sha256="):
            msg = "Missing/invalid X-Hub-Signature-256 header on POST /webhook"
            if strict:
                log.warning("%s (strict mode)", msg)
                return False
            log.warning("%s; allowing POST because WEBHOOK_STRICT_SIGNATURE=false", msg)
            return True
        provided = signature_header.split("=", 1)[1].strip()
        expected = hmac.new(
            app_secret.encode("utf-8"),
            msg=raw_body,
            digestmod=hashlib.sha256,
        ).hexdigest()
        ok = hmac.compare_digest(provided, expected)
        if not ok:
            if strict:
                log.warning("Signature mismatch on POST /webhook (strict mode)")
                return False
            log.warning("Signature mismatch on POST /webhook; allowing because WEBHOOK_STRICT_SIGNATURE=false")
            return True
        return ok

    def do_HEAD(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/webhook"):
            self.send_response(404)
            self.end_headers()
            return
        # Lightweight health/probe response.
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/webhook"):
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"not_found"}')
            return

        # Meta webhook verification flow:
        # GET /webhook?hub.mode=subscribe&hub.challenge=...&hub.verify_token=...
        params = parse_qs(parsed.query or "")
        hub_mode = (params.get("hub.mode") or params.get("hub_mode") or [""])[0]
        hub_challenge = (params.get("hub.challenge") or params.get("hub_challenge") or [""])[0]
        hub_verify_token = (
            params.get("hub.verify_token")
            or params.get("hub_verify_token")
            or [""]
        )[0]
        expected_verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()
        if hub_mode == "subscribe":
            if not expected_verify_token:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"missing_server_verify_token"}')
                return
            if hub_verify_token == expected_verify_token:
                body = hub_challenge.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"invalid_verify_token"}')
            return

        # Non-Meta GET: keep existing behavior (generate/serve PDF)
        if not self._is_authorized_non_meta_get(parsed):
            body = b'{"error":"unauthorized"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            pdf_path = _get_or_generate_pdf()
            if not pdf_path.exists():
                raise FileNotFoundError(f"PDF not found: {pdf_path}")

            file_bytes = pdf_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'inline; filename="{pdf_path.name}"')
            self.send_header("Content-Length", str(len(file_bytes)))
            self.end_headers()
            self.wfile.write(file_bytes)
        except Exception as e:
            log.exception("Webhook PDF generation failed")
            body = f'{{"error":"generation_failed","message":"{str(e)}"}}'.encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/webhook"):
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"not_found"}')
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length) if content_length > 0 else b""
            if not self._verify_meta_signature(raw_body):
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"invalid_signature"}')
                return

            # At minimum acknowledge Meta quickly with 200.
            # Log payload for processing pipelines if needed.
            if raw_body:
                log.info("Received webhook payload bytes=%s", len(raw_body))

            response = {"status": "ok"}
            try:
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
                self._append_webhook_audit(payload)
                texts = self._extract_text_messages(payload)
                if texts:
                    log.info("Webhook text messages: %s", texts)
                incoming = self._extract_incoming_text_messages(payload)
                log.info("Webhook incoming parsed messages: %s", incoming)
                for msg in incoming:
                    sender = msg["from"]
                    body = msg["body"]
                    msg_id = msg.get("id", "")
                    timestamp = msg.get("timestamp", "")
                    if self._is_stale_message(timestamp, max_age_seconds=180):
                            log.warning("Skipped stale message from %s", sender)
                            continue
                    if self._is_duplicate_message_id(msg_id):
                        log.info("Skipping duplicate webhook message id=%s sender=%s", msg_id, sender)
                        continue
                    log.info("Evaluating inbound message from=%s id=%s body=%s", sender, msg_id, body)
                    body_clean = body.strip().lower()
                    whitelist = self._parse_whitelist_numbers()
                    if whitelist and sender not in whitelist:
                        log.info(
                            "Ignoring message from non-whitelisted number %s; allowed=%s",
                            sender,
                            sorted(whitelist),
                        )
                        continue

                    if body_clean == "fof":
                        if self._send_whatsapp_template(sender, "ifmis_menu", "en"):
                            log.info("Sent template 'ifmis_menu' to %s", sender)
                            response["message"] = "template_ifmis_menu_sent"
                        else:
                            log.warning("Template 'ifmis_menu' send failed for %s", sender)
                        continue

                    if body_clean == "ssai":
                        if self._send_whatsapp_template(sender, "ifmis_menu_v2", "en"):
                            log.info("Sent template 'ifmis_menu_v2' to %s", sender)
                            response["message"] = "template_ssel_use_menu_option_sent"
                        else:
                            log.warning("Template 'ifmis_menu_v2' send failed for %s", sender)
                        continue

                    weekly_report_triggers = {
                        "weekly report",
                        "weekly status",
                        "weekly_report",
                        "weekly_status",
                    }
                    if body_clean in weekly_report_triggers:
                        if not self._should_send_weekly_report(sender, cooldown_seconds=120):
                            log.info("Skipping weekly report resend for %s within cooldown window", sender)
                            continue
                        try:
                            self._send_whatsapp_text(
                                sender,
                                "Please wait, your weekly report is being prepared. It will be delivered shortly.",
                            )
                            pdf_path = _get_or_generate_pdf()
                            if self._send_whatsapp_document(sender, pdf_path):
                                log.info("Sent weekly report PDF %s to %s", pdf_path.name, sender)
                                response["message"] = "weekly_report_sent"
                            else:
                                log.warning("Weekly report send failed for %s", sender)
                        except Exception as e:
                            log.error("Weekly report generation/send failed for %s: %s", sender, e)
                        continue

                    if body_clean == "check token status":
                        if self._send_whatsapp_text(sender, "Please enter token number"):
                            log.info("Prompted token number entry for %s", sender)
                            response["message"] = "token_prompt_sent"
                        else:
                            log.warning("Token prompt send failed for %s", sender)
                        continue

                    if body_clean == "payment status":
                        payment_msg = (
                            "Payment Status:\n"
                            "  Total Sent for Payment: 10 Cr\n"
                            "  Total DN Received: 5 Cr\n"
                            "  Total RN Received: 1 Cr\n"
                            "  At the Bank: 4 Cr"
                        )
                        if self._send_whatsapp_text(sender, payment_msg):
                            log.info("Sent payment status to %s", sender)
                            response["message"] = "payment_status_sent"
                        else:
                            log.warning("Payment status send failed for %s", sender)
                        continue

                    token_candidate = body.strip()
                    if re.fullmatch(r"\d{10}", token_candidate):
                        token_msg = (
                            "Token details:\n"
                            f"  Token number: {token_candidate}\n"
                            "  Total Amount: 0.02 Cr\n"
                            "  Total Parties: 10\n"
                            "  Description: Employee Salary\n"
                            "  Status: Pending for Government Approval"
                        )
                        if self._send_whatsapp_text(sender, token_msg):
                            log.info("Sent token details for token=%s to %s", token_candidate, sender)
                            response["message"] = "token_details_sent"
                        else:
                            log.warning("Token details send failed for %s", sender)
                        continue

                    if self._is_greeting(body):
                        if not self._should_reply_now(sender, dedupe_seconds=30):
                            log.info("Skipped duplicate greeting reply to %s within dedupe window", sender)
                            continue
                        if self._send_whatsapp_template(sender, "ifmis_menu_v2", "en"):
                            log.info("Sent template 'ifmis_menu_v2' to %s for greeting", sender)
                            response["message"] = "template_ifmis_menu_v2_sent"
                        else:
                            log.warning("Template 'ifmis_menu_v2' send failed for greeting %s", sender)
            except Exception as e:
                log.exception("POST message processing failed: %s", e)

            body = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            log.exception("Webhook POST handling failed: %s", e)
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"post_handler_failed"}')

    def log_message(self, fmt, *args):
        # Route HTTP server logs through project logger instead of stderr.
        log.info("webhook %s", fmt % args)



def main():
    port = int(os.getenv("WEBHOOK_PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), WebhookHandler)
    log.info("Webhook server listening on :%s (GET /webhook)", port)
    print(f"Webhook server listening on :{port} (GET /webhook)")
    server.serve_forever()


if __name__ == "__main__":
    main()
