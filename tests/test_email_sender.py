import base64
import json

import httpx
import pytest

from collection_agent.email_sender import (
    POSTMARK_URL,
    Attachment,
    EmailSendError,
    OutgoingEmail,
    PostmarkSender,
)

TOKEN = "pm_test_fake_not_real"


def message() -> OutgoingEmail:
    return OutgoingEmail(
        from_name='Acme "Billing", Co',
        from_address="billing@billing.example.test",
        to="ap@acme.example",
        reply_to="reply+abc@billing.example.test",
        subject="Invoice X",
        text_body="Hello\n",
        attachments=(Attachment("c.pdf", b"%PDF-1.7 bytes", "application/pdf"),),
        metadata={"invoice_id": "i1", "outbound_email_id": "o1"},
    )


def sender_with(handler: httpx.MockTransport) -> PostmarkSender:
    return PostmarkSender(TOKEN, "outbound", httpx.Client(transport=handler))


def test_request_shape() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ErrorCode": 0, "Message": "OK", "MessageID": "m-1"})

    sent = sender_with(httpx.MockTransport(handler)).send(message())

    assert sent.provider_message_id == "m-1"
    (req,) = seen
    assert req.method == "POST"
    assert str(req.url) == POSTMARK_URL == "https://api.postmarkapp.com/email"
    assert req.headers["X-Postmark-Server-Token"] == TOKEN
    assert req.headers["Accept"] == "application/json"
    assert req.headers["Content-Type"] == "application/json"
    assert json.loads(req.content) == {
        "From": '"Acme \\"Billing\\", Co" <billing@billing.example.test>',
        "To": "ap@acme.example",
        "ReplyTo": "reply+abc@billing.example.test",
        "Subject": "Invoice X",
        "TextBody": "Hello\n",
        "MessageStream": "outbound",
        "Attachments": [
            {
                "Name": "c.pdf",
                "Content": base64.b64encode(b"%PDF-1.7 bytes").decode(),
                "ContentType": "application/pdf",
            }
        ],
        "Metadata": {"invoice_id": "i1", "outbound_email_id": "o1"},
        "TrackOpens": False,
        "TrackLinks": "None",
    }


def test_http_422_with_error_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"ErrorCode": 300, "Message": "Invalid email request"})

    with pytest.raises(EmailSendError) as info:
        sender_with(httpx.MockTransport(handler)).send(message())
    assert info.value.error_code == 300
    assert info.value.outcome_unknown is False
    assert "300" in str(info.value)
    assert TOKEN not in str(info.value)


def test_200_with_nonzero_error_code_is_a_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ErrorCode": 406, "Message": "Inactive recipient"})

    with pytest.raises(EmailSendError) as info:
        sender_with(httpx.MockTransport(handler)).send(message())
    assert info.value.error_code == 406


def test_5xx_and_garbage_are_outcome_unknown() -> None:
    def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"ErrorCode": 100, "Message": "boom"})

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>")

    for handler in (server_error, garbage):
        with pytest.raises(EmailSendError) as info:
            sender_with(httpx.MockTransport(handler)).send(message())
        assert info.value.outcome_unknown is True


def test_timeout_is_unknown_and_connect_error_is_not_and_token_never_leaks() -> None:
    def read_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timed out token={TOKEN}", request=request)

    def connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused token={TOKEN}", request=request)

    with pytest.raises(EmailSendError) as unknown:
        sender_with(httpx.MockTransport(read_timeout)).send(message())
    assert unknown.value.outcome_unknown is True
    assert TOKEN not in str(unknown.value)

    with pytest.raises(EmailSendError) as definite:
        sender_with(httpx.MockTransport(connect_error)).send(message())
    assert definite.value.outcome_unknown is False
    assert TOKEN not in str(definite.value)


def test_no_automatic_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={"ErrorCode": 100, "Message": "x"})

    with pytest.raises(EmailSendError):
        sender_with(httpx.MockTransport(handler)).send(message())
    assert calls == 1


def test_threading_headers_are_sent_as_postmark_headers_only_when_present() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ErrorCode": 0, "Message": "OK", "MessageID": "m-1"})

    sender = sender_with(httpx.MockTransport(handler))
    threaded = OutgoingEmail(
        from_name="Acme",
        from_address="billing@billing.example.test",
        to="ap@acme.example",
        reply_to="reply+abc@billing.example.test",
        subject="Re: x",
        text_body="Hello\n",
        headers=(("In-Reply-To", "<a@x>"), ("References", "<z@x> <a@x>")),
    )
    sender.send(threaded)
    sender.send(message())

    assert json.loads(seen[0].content)["Headers"] == [
        {"Name": "In-Reply-To", "Value": "<a@x>"},
        {"Name": "References", "Value": "<z@x> <a@x>"},
    ]
    assert "Headers" not in json.loads(seen[1].content)
