"""Parsing a Postmark inbound payload. Everything in it is untrusted input, so every accessor
tolerates missing or wrongly typed fields instead of raising."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Any

MAX_CLASSIFIED_CHARS = 4_000
MAX_STORED_BODY_CHARS = 100_000
MAX_MESSAGE_ID_CHARS = 998
MAX_SUBJECT_STORED = 1_000

AUTO_SUBJECT_PREFIXES = ("automatic reply", "auto:", "out of office", "autoreply")
AUTO_PRECEDENCE = frozenset({"auto_reply", "bulk", "junk", "list"})

_ANGLE_ID = re.compile(r"<([^<>\s]+)>")
_ORIGINAL_MESSAGE = re.compile(r"^\s*-{2,}\s*original message\s*-{2,}\s*$", re.IGNORECASE)
_ON_WROTE = re.compile(r"^On\s.*wrote:\s*$", re.IGNORECASE)
_ENDS_WROTE = re.compile(r"wrote:\s*$", re.IGNORECASE)


def _text(value: Any) -> str:
    """A string field, or "". NUL characters are dropped: Postgres text cannot hold them."""
    return value.replace("\x00", "") if isinstance(value, str) else ""


@dataclass(frozen=True)
class InboundEmail:
    from_address: str
    subject: str
    message_id: str  # the dedupe key: the Message-ID header, or `postmark:<MessageID>`
    postmark_message_id: str | None
    text_body: str
    stripped_reply: str
    headers: Mapping[str, str]  # lower-cased names; repeated headers joined with a space
    mailbox_hashes: frozenset[str]
    attachment_count: int

    @property
    def threading_text(self) -> str:
        return f"{self.headers.get('in-reply-to', '')} {self.headers.get('references', '')}".strip()


def _header_map(raw: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                name, value = _text(item.get("Name")).strip().lower(), _text(item.get("Value"))
                if name:
                    headers[name] = f"{headers[name]} {value}".strip() if name in headers else value
    return headers


def normalise_message_id(value: str) -> str:
    """The id without angle brackets or surrounding whitespace."""
    found = _ANGLE_ID.search(value)
    return (found.group(1) if found else value.strip().strip("<>").strip())[:MAX_MESSAGE_ID_CHARS]


def parse_inbound(payload: Any) -> InboundEmail | None:
    """None if the payload has neither a Message-ID header nor a Postmark MessageID."""
    if not isinstance(payload, dict):
        return None
    headers = _header_map(payload.get("Headers"))
    postmark_id = _text(payload.get("MessageID")).strip() or None
    header_id = normalise_message_id(headers.get("message-id", ""))
    if header_id:
        message_id = header_id
    elif postmark_id:
        message_id = f"postmark:{postmark_id}"
    else:
        return None

    from_full = payload.get("FromFull")
    address = _text(from_full.get("Email")) if isinstance(from_full, dict) else ""
    if not address:
        address = parseaddr(_text(payload.get("From")))[1]

    hashes = {_text(payload.get("MailboxHash")).strip()}
    to_full = payload.get("ToFull")
    if isinstance(to_full, list):
        hashes.update(_text(t.get("MailboxHash")).strip() for t in to_full if isinstance(t, dict))
    attachments = payload.get("Attachments")
    return InboundEmail(
        from_address=address.strip().lower() or "unknown",
        subject=_text(payload.get("Subject"))[:MAX_SUBJECT_STORED],
        message_id=message_id,
        postmark_message_id=postmark_id,
        text_body=_text(payload.get("TextBody")),
        stripped_reply=_text(payload.get("StrippedTextReply")),
        headers=headers,
        mailbox_hashes=frozenset(h for h in hashes if h),
        attachment_count=len(attachments) if isinstance(attachments, list) else 0,
    )


def is_auto_reply(headers: Mapping[str, str], subject: str) -> bool:
    auto_submitted = headers.get("auto-submitted")
    if auto_submitted is not None and auto_submitted.strip().lower() != "no":
        return True
    if "x-autoreply" in headers or "x-autorespond" in headers:
        return True
    if headers.get("precedence", "").strip().lower() in AUTO_PRECEDENCE:
        return True
    return subject.strip().lower().startswith(AUTO_SUBJECT_PREFIXES)


def extract_reply_text(stripped_reply: str, text_body: str) -> str:
    """The customer's new words only: `StrippedTextReply` if present, else `TextBody`; quoted
    lines and everything after the first reply separator removed; at most 4,000 characters."""
    source = stripped_reply if stripped_reply.strip() else text_body
    lines = source.splitlines()
    kept: list[str] = []
    for i, line in enumerate(lines):
        if _is_separator(line, lines[i + 1] if i + 1 < len(lines) else ""):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()[:MAX_CLASSIFIED_CHARS]


def _is_separator(line: str, next_line: str) -> bool:
    if _ORIGINAL_MESSAGE.match(line) or line.startswith("From: "):
        return True
    if _ON_WROTE.match(line):
        return True
    # "On Mon, 5 Oct 2026 at 10:00, Jo Bloggs <jo@example.test>\nwrote:" wrapped over two lines
    return line.startswith("On ") and len(line) < 200 and bool(_ENDS_WROTE.search(next_line))
