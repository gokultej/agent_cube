"""
Email / Slack / WhatsApp delivery for the generated PDF (optional via environment).
"""
from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import mimetypes
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict

import requests

log = logging.getLogger(__name__)

_PLACEHOLDER_MARKERS = (
    "yourcompany.com",
    "your_password",
    "xxx/yyy/zzz",
    "your_token",
    "your_whatsapp_api_url",
)


def _env_clean(key: str, default: str = "") -> str:
    """Strip whitespace and optional outer quotes from dotenv-loaded values."""
    v = (os.getenv(key) or default).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


def _whatsapp_messages_to_media_url(messages_url: str) -> str:
    u = messages_url.rstrip("/")
    if u.endswith("/messages"):
        return u[: -len("/messages")] + "/media"
    return ""


def _extract_graph_phone_id(url: str) -> str | None:
    m = re.search(r"/v\d+\.\d+/(\d+)/(?:messages|media)(?:/|$|\?)", url)
    return m.group(1) if m else None


def _resolve_whatsapp_media_upload_url(messages_url: str, env_media: str) -> str:
    """
    Prefer WHATSAPP_MEDIA_UPLOAD_URL when it matches the phone id in messages URL;
    otherwise derive .../<id>/media from WHATSAPP_API_URL (avoids 404 from mismatched ids).
    """
    derived = _whatsapp_messages_to_media_url(messages_url)
    mid = _extract_graph_phone_id(messages_url)
    eid = _extract_graph_phone_id(env_media) if env_media else None
    if env_media and eid and mid and eid != mid:
        log.warning(
            "WHATSAPP_MEDIA_UPLOAD_URL id %s != WHATSAPP_API_URL id %s; using media URL derived from messages URL.",
            eid,
            mid,
        )
        return derived or env_media
    if env_media.strip():
        return env_media.strip()
    return derived


def _whatsapp_template_language() -> str:
    return (
        _env_clean("WHATSAPP_TEMPLATE_LANG")
        or _env_clean("WHATSAPP_TEMPLATE_LANGUAGE")
        or "en"
    )


def _looks_configured_smtp() -> bool:
    host = (os.getenv("SMTP_HOST") or "").strip()
    user = (os.getenv("SMTP_USER") or "").strip()
    pwd = (os.getenv("SMTP_PASSWORD") or "").strip()
    if not host or not user or not pwd:
        return False
    combined = f"{host}{pwd}".lower()
    return not any(p in combined for p in _PLACEHOLDER_MARKERS)


def _looks_configured_slack() -> bool:
    url = (os.getenv("SLACK_WEBHOOK") or "").strip()
    return bool(url) and "hooks.slack.com" in url and "xxx" not in url


def _parse_whatsapp_numbers(raw: str) -> list[str]:
    """
    Accept either:
    - JSON array string: ["919876543210","918888777666"]
    - comma-separated string: 919876543210,918888777666
    """
    raw = (raw or "").strip()
    if not raw:
        return []

    numbers: list[str] = []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                numbers = [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            numbers = []
    if not numbers:
        numbers = [x.strip() for x in raw.split(",") if x.strip()]
    return numbers


def _looks_configured_whatsapp() -> bool:
    api_url = _env_clean("WHATSAPP_API_URL")
    token = _env_clean("WHATSAPP_API_TOKEN")
    numbers = _parse_whatsapp_numbers(os.getenv("WHATSAPP_TO_NUMBERS", ""))
    if not api_url or not token or not numbers:
        return False
    if re.search(r"/v\d+\.\d+/[^/]*[xX]{2,}[^/]*/", api_url):
        return False
    combined = f"{api_url}{token}".lower()
    return not any(p in combined for p in _PLACEHOLDER_MARKERS)


def distribute_report(
    pdf_path: str,
    sales_kpis: Dict[str, Any],
    claude_output: Dict[str, Any],
) -> None:
    """
    Send PDF by email and/or notify Slack/WhatsApp when credentials are configured.
    """
    pdf_path = str(pdf_path)
    if not Path(pdf_path).is_file():
        log.error("PDF not found for distribution: %s", pdf_path)
        return

    if _looks_configured_smtp():
        _send_email(pdf_path, sales_kpis, claude_output)
    else:
        log.info("SMTP not configured (or still placeholder) — skipping email.")

    if _looks_configured_slack():
        _post_slack(pdf_path, sales_kpis, claude_output)
    else:
        log.info("Slack webhook not configured — skipping Slack notification.")

    if _looks_configured_whatsapp():
        try:
            _post_whatsapp(pdf_path, sales_kpis, claude_output)
        except Exception as e:
            log.error("WhatsApp distribution failed: %s", e)
    else:
        log.info("WhatsApp API not configured — skipping WhatsApp notification.")


def _send_email(
    pdf_path: str,
    sales_kpis: Dict[str, Any],
    claude_output: Dict[str, Any],
) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    to_raw = os.getenv("EMAIL_TO", "").strip()
    recipients = [e.strip() for e in to_raw.split(",") if e.strip()]

    if not recipients:
        log.warning("EMAIL_TO empty — cannot send email.")
        return

    total_cr = float(sales_kpis.get("total_net_sales_cr") or 0)
    summary_lines = claude_output.get("executive_summary") or []
    if isinstance(summary_lines, str):
        preview = summary_lines[:400]
    else:
        preview = "\n".join(str(x) for x in summary_lines[:5])[:800]

    msg = EmailMessage()
    msg["Subject"] = f"Weekly intelligence report — net sales Rs.{total_cr:.2f} Cr"
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.set_content(
        f"Weekly report attached.\n\nExecutive summary (preview):\n{preview}\n"
    )

    with open(pdf_path, "rb") as f:
        msg.add_attachment(
            f.read(),
            maintype="application",
            subtype="pdf",
            filename=Path(pdf_path).name,
        )

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
        log.info("Email sent to %s", recipients)
    except Exception as e:
        log.error("Email send failed: %s", e)


def _post_slack(
    pdf_path: str,
    sales_kpis: Dict[str, Any],
    claude_output: Dict[str, Any],
) -> None:
    url = os.getenv("SLACK_WEBHOOK", "").strip()
    total_cr = float(sales_kpis.get("total_net_sales_cr") or 0)
    lines = claude_output.get("executive_summary") or []
    if isinstance(lines, list):
        text_preview = " ".join(str(x) for x in lines[:2])[:300]
    else:
        text_preview = str(lines)[:300]

    payload = {
        "text": (
            f"*Weekly intelligence PDF generated*\n"
            f"• Net sales (week): Rs.{total_cr:.2f} Cr\n"
            f"• File: `{Path(pdf_path).name}`\n"
            f"• Summary: {text_preview}"
        )
    }
    try:
        r = requests.post(url, data=json.dumps(payload), headers={"Content-Type": "application/json"}, timeout=15)
        r.raise_for_status()
        log.info("Slack notification posted.")
    except Exception as e:
        log.error("Slack webhook failed: %s", e)


def _post_whatsapp(
    pdf_path: str,
    sales_kpis: Dict[str, Any],
    claude_output: Dict[str, Any],
) -> None:
    """
    Send WhatsApp text message to multiple recipients.

    Expected env vars:
      WHATSAPP_API_URL      e.g. https://graph.facebook.com/v19.0/<phone_number_id>/messages
      WHATSAPP_API_TOKEN    bearer token for provider
      WHATSAPP_TO_NUMBERS   JSON array or CSV numbers
    """
    api_url = _env_clean("WHATSAPP_API_URL")
    token = _env_clean("WHATSAPP_API_TOKEN")
    numbers = _parse_whatsapp_numbers(os.getenv("WHATSAPP_TO_NUMBERS", ""))
    template_name = (os.getenv("WHATSAPP_TEMPLATE_NAME") or "").strip()

    total_cr = float(sales_kpis.get("total_net_sales_cr") or 0)
    lines = claude_output.get("executive_summary") or []
    if isinstance(lines, list):
        summary = " ".join(str(x) for x in lines[:2])[:280]
    else:
        summary = str(lines)[:280]
    msg_text = (
        "Weekly intelligence report generated.\n"
        f"Net sales (week): Rs.{total_cr:.2f} Cr\n"
        f"File: {Path(pdf_path).name}\n"
        f"Summary: {summary}"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # If approved template is configured, send template with PDF attachment.
    if template_name:
        _post_whatsapp_template_document(
            api_url=api_url,
            token=token,
            numbers=numbers,
            pdf_path=pdf_path,
            sales_kpis=sales_kpis,
            claude_output=claude_output,
        )
        return

    sent = 0
    for number in numbers:
        payload = {
            "messaging_product": "whatsapp",
            "to": number,
            "type": "text",
            "text": {"body": msg_text},
        }
        try:
            r = requests.post(api_url, data=json.dumps(payload), headers=headers, timeout=20)
            r.raise_for_status()
            sent += 1
        except Exception as e:
            log.error("WhatsApp send failed for %s: %s", number, e)

    log.info("WhatsApp messages sent: %s/%s", sent, len(numbers))


def _upload_whatsapp_media(token: str, media_upload_url: str, pdf_path: str) -> str:
    """
    Upload a PDF to WhatsApp Cloud API media endpoint and return media_id.
    media_upload_url example:
      https://graph.facebook.com/v19.0/<PHONE_NUMBER_ID>/media
    """
    mime_type = mimetypes.guess_type(pdf_path)[0] or "application/pdf"
    with open(pdf_path, "rb") as f:
        files = {
            "file": (Path(pdf_path).name, f, mime_type)
        }
        data = {
            "messaging_product": "whatsapp",
            "type": mime_type,
        }
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.post(media_upload_url, headers=headers, data=data, files=files, timeout=60)
        r.raise_for_status()
        payload = r.json()
        media_id = payload.get("id")
        if not media_id:
            raise RuntimeError(f"Media upload succeeded but id missing: {payload}")
        return media_id


def _post_whatsapp_template_document(
    api_url: str,
    token: str,
    numbers: list[str],
    pdf_path: str,
    sales_kpis: Dict[str, Any],
    claude_output: Dict[str, Any],
) -> None:
    """
    Send approved WhatsApp template with PDF document header.

    Required env:
      WHATSAPP_TEMPLATE_NAME
      WHATSAPP_TEMPLATE_LANG (default: en)
      WHATSAPP_MEDIA_UPLOAD_URL

    Optional env:
      WHATSAPP_TEMPLATE_BODY_VARS JSON array of strings for templates with {{1}}, {{2}}, …
      placeholders. If unset or [], the body component is sent as {"type": "body"} only
      (matches templates like week_report2 with no body variables).
    """
    template_name = (os.getenv("WHATSAPP_TEMPLATE_NAME") or "").strip()
    lang = _whatsapp_template_language()
    env_media = _env_clean("WHATSAPP_MEDIA_UPLOAD_URL")
    media_upload_url = _resolve_whatsapp_media_upload_url(api_url, env_media)
    body_vars_raw = (os.getenv("WHATSAPP_TEMPLATE_BODY_VARS") or "").strip()

    if not media_upload_url:
        raise RuntimeError(
            "WhatsApp media URL missing: set WHATSAPP_API_URL to .../<PHONE_NUMBER_ID>/messages "
            "or set WHATSAPP_MEDIA_UPLOAD_URL to .../<same PHONE_NUMBER_ID>/media"
        )

    body_vars: list[str] = []
    if body_vars_raw:
        try:
            parsed = json.loads(body_vars_raw)
            if not isinstance(parsed, list):
                raise ValueError("WHATSAPP_TEMPLATE_BODY_VARS must be a JSON array")
            body_vars = [str(x) for x in parsed]
        except Exception as e:
            raise RuntimeError(f"Invalid WHATSAPP_TEMPLATE_BODY_VARS: {e}")

    media_id = _upload_whatsapp_media(token, media_upload_url, pdf_path)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    header_component: Dict[str, Any] = {
        "type": "header",
        "parameters": [
            {
                "type": "document",
                "document": {
                    "id": media_id,
                    "filename": Path(pdf_path).name,
                },
            }
        ],
    }
    if body_vars:
        body_component: Dict[str, Any] = {
            "type": "body",
            "parameters": [{"type": "text", "text": v} for v in body_vars],
        }
    else:
        body_component = {"type": "body"}

    sent = 0
    for number in numbers:
        payload = {
            "messaging_product": "whatsapp",
            "to": number,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": lang},
                "components": [header_component, body_component],
            },
        }
        try:
            r = requests.post(api_url, data=json.dumps(payload), headers=headers, timeout=20)
            r.raise_for_status()
            sent += 1
        except Exception as e:
            log.error("WhatsApp template send failed for %s: %s", number, e)

    log.info("WhatsApp template messages sent: %s/%s", sent, len(numbers))
