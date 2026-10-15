"""Sending email. `EmailSender` is the seam; `PostmarkSender` is the only real implementation.

No automatic retries anywhere: a retried send could put a duplicate in a customer's inbox.
"""

import base64
from dataclasses import dataclass, field
from email.utils import formataddr
from typing import Protocol

import httpx

POSTMARK_URL = "https://api.postmarkapp.com/email"
TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class Attachment:
    name: str
    content: bytes
    content_type: str


@dataclass(frozen=True)
class OutgoingEmail:
    from_name: str
    from_address: str
    to: str
    reply_to: str
    subject: str
    text_body: str
    attachments: tuple[Attachment, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)
    headers: tuple[tuple[str, str], ...] = ()  # extra headers, e.g. In-Reply-To / References


@dataclass(frozen=True)
class SentEmail:
    provider_message_id: str


class EmailSendError(Exception):
    """A send failed. Never carries credentials.

    `outcome_unknown` is True when the provider may have accepted the message anyway (a timeout
    after the request went out, a 5xx, an unreadable reply). Callers must not re-send then.
    """

    def __init__(
        self, message: str, *, error_code: int | None = None, outcome_unknown: bool = False
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.outcome_unknown = outcome_unknown


class EmailSender(Protocol):
    def send(self, message: OutgoingEmail) -> SentEmail: ...


class PostmarkSender:
    def __init__(
        self, server_token: str, message_stream: str, http_client: httpx.Client | None = None
    ) -> None:
        self._token = server_token
        self._stream = message_stream
        self._client = http_client or httpx.Client(timeout=TIMEOUT_SECONDS)

    def _payload(self, message: OutgoingEmail) -> dict[str, object]:
        payload: dict[str, object] = {
            "From": formataddr((message.from_name, message.from_address)),
            "To": message.to,
            "ReplyTo": message.reply_to,
            "Subject": message.subject,
            "TextBody": message.text_body,
            "MessageStream": self._stream,
            "Attachments": [
                {
                    "Name": a.name,
                    "Content": base64.b64encode(a.content).decode("ascii"),
                    "ContentType": a.content_type,
                }
                for a in message.attachments
            ],
            "Metadata": message.metadata,
            "TrackOpens": False,  # keep the payment link untouched
            "TrackLinks": "None",
        }
        if message.headers:
            payload["Headers"] = [{"Name": n, "Value": v} for n, v in message.headers]
        return payload

    def send(self, message: OutgoingEmail) -> SentEmail:
        try:
            response = self._client.post(
                POSTMARK_URL,
                json=self._payload(message),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Postmark-Server-Token": self._token,
                },
                timeout=TIMEOUT_SECONDS,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # The request never reached Postmark.
            raise EmailSendError(f"could not connect to Postmark: {type(exc).__name__}") from None
        except httpx.HTTPError as exc:
            raise EmailSendError(
                f"no usable reply from Postmark: {type(exc).__name__}", outcome_unknown=True
            ) from None

        try:
            data = response.json()
            code = data["ErrorCode"]
            detail = str(data.get("Message", ""))[:200]
        except ValueError, KeyError, TypeError:
            raise EmailSendError(
                f"unreadable reply from Postmark (HTTP {response.status_code})",
                outcome_unknown=True,
            ) from None

        if response.status_code == 200 and code == 0:
            message_id = data.get("MessageID")
            if not isinstance(message_id, str) or not message_id:
                raise EmailSendError("Postmark reply had no MessageID", outcome_unknown=True)
            return SentEmail(provider_message_id=message_id)
        raise EmailSendError(
            f"Postmark rejected the send: HTTP {response.status_code} ErrorCode {code}: {detail}",
            error_code=code if isinstance(code, int) else None,
            outcome_unknown=response.status_code >= 500,
        )
