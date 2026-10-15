"""Pure parsing of Postmark payloads: no database."""

from collection_agent.inbound import (
    MAX_CLASSIFIED_CHARS,
    extract_reply_text,
    is_auto_reply,
    normalise_message_id,
    parse_inbound,
)
from tests.inbound_helpers import headers, payload

# --- body extraction ---------------------------------------------------------------------------


def test_stripped_reply_is_preferred_when_non_empty() -> None:
    assert extract_reply_text("Just this", "Just this\n\nOn Mon wrote:\n> old") == "Just this"


def test_falls_back_to_text_body_when_stripped_is_empty_or_blank() -> None:
    assert extract_reply_text("", "Whole body") == "Whole body"
    assert extract_reply_text("  \n ", "Whole body") == "Whole body"


def test_quoted_lines_are_removed() -> None:
    body = "We will pay Friday.\n> Invoice INV-1 is due\n  > indented quote\nThanks"
    assert extract_reply_text("", body) == "We will pay Friday.\nThanks"


def test_cuts_at_on_date_wrote() -> None:
    body = (
        "Paid yesterday.\n\nOn Mon, 5 Oct 2026 at 10:00, Billing <b@example.test> wrote:\nOld text"
    )
    assert extract_reply_text("", body) == "Paid yesterday."


def test_cuts_at_wrapped_on_wrote() -> None:
    body = "Paid.\n\nOn Mon, 5 Oct 2026 at 10:00, Billing <b@example.test>\nwrote:\nOld text"
    assert extract_reply_text("", body) == "Paid."


def test_cuts_at_original_message_separator() -> None:
    body = "Will pay soon.\n-----Original Message-----\nFrom: x\nOld"
    assert extract_reply_text("", body) == "Will pay soon."
    assert extract_reply_text("", "Soon.\n--- original message ---\nOld") == "Soon."


def test_cuts_at_from_line() -> None:
    assert extract_reply_text("", "Thanks.\nFrom: Billing <b@example.test>\nOld") == "Thanks."


def test_a_line_merely_containing_on_or_from_is_kept() -> None:
    body = "We paid on 5 Oct, wrote the cheque from home."
    assert extract_reply_text("", body) == body


def test_truncated_to_4000_characters() -> None:
    assert len(extract_reply_text("", "a" * 10_000)) == MAX_CLASSIFIED_CHARS


def test_only_quoted_text_gives_empty() -> None:
    assert extract_reply_text("", "> all old\n> text") == ""


# --- auto replies --------------------------------------------------------------------------------


def test_each_auto_reply_signal() -> None:
    assert is_auto_reply({"auto-submitted": "auto-replied"}, "Re: x")
    assert is_auto_reply({"auto-submitted": "auto-generated"}, "Re: x")
    assert is_auto_reply({"x-autoreply": "yes"}, "Re: x")
    assert is_auto_reply({"x-autorespond": ""}, "Re: x")
    for value in ("auto_reply", "bulk", "junk", "list", " Bulk "):
        assert is_auto_reply({"precedence": value}, "Re: x")
    for subject in (
        "Automatic reply: x",
        "Auto: x",
        "Out of office: x",
        "Autoreply x",
        " OUT OF OFFICE",
    ):
        assert is_auto_reply({}, subject)


def test_ordinary_mail_is_not_an_auto_reply() -> None:
    assert not is_auto_reply({}, "Re: Invoice INV-1")
    assert not is_auto_reply({"auto-submitted": "no"}, "Re: x")
    assert not is_auto_reply({"auto-submitted": " No "}, "Re: x")
    assert not is_auto_reply({"precedence": "first-class"}, "Re: x")
    assert not is_auto_reply({}, "I am out of office next week but will pay")


# --- parse_inbound -------------------------------------------------------------------------------


def test_message_id_header_is_the_dedupe_key_without_brackets() -> None:
    email = parse_inbound(payload(message_id="<abc123@mail.test>"))
    assert email is not None
    assert email.message_id == "abc123@mail.test"


def test_header_names_are_case_insensitive() -> None:
    raw = payload(message_id=None, extra_headers=[{"Name": "MESSAGE-id", "Value": " <X@y> "}])
    email = parse_inbound(raw)
    assert email is not None
    assert email.message_id == "X@y"


def test_falls_back_to_postmark_message_id() -> None:
    email = parse_inbound(payload(message_id=None, postmark_id="pm-1234"))
    assert email is not None
    assert email.message_id == "postmark:pm-1234"
    assert email.postmark_message_id == "pm-1234"


def test_no_ids_at_all_is_unparseable() -> None:
    raw = payload(message_id=None)
    raw["MessageID"] = ""
    assert parse_inbound(raw) is None
    assert parse_inbound("not a dict") is None
    assert parse_inbound(None) is None


def test_sender_is_lowercased_and_falls_back_to_the_from_header() -> None:
    email = parse_inbound(payload(from_email="AP@Acme.TEST"))
    assert email is not None
    assert email.from_address == "ap@acme.test"
    raw = payload()
    del raw["FromFull"]
    raw["From"] = "Jo <JO@Example.test>"
    again = parse_inbound(raw)
    assert again is not None
    assert again.from_address == "jo@example.test"


def test_mailbox_hashes_collected_from_top_level_and_to_full() -> None:
    raw = payload(mailbox_hash="tok1")
    raw["ToFull"].append({"Email": "x@y", "MailboxHash": "tok2"})
    email = parse_inbound(raw)
    assert email is not None
    assert email.mailbox_hashes == {"tok1", "tok2"}


def test_attachments_are_counted_not_kept() -> None:
    email = parse_inbound(payload(attachments=3))
    assert email is not None
    assert email.attachment_count == 3


def test_threading_text_joins_in_reply_to_and_references() -> None:
    email = parse_inbound(payload(extra_headers=headers(In_Reply_To="<a@b>", References="<c@d>")))
    assert email is not None
    assert email.threading_text == "<a@b> <c@d>"


def test_wrongly_typed_fields_do_not_raise() -> None:
    raw = payload()
    raw["Headers"] = "nope"
    raw["ToFull"] = 5
    raw["Attachments"] = None
    raw["Subject"] = 12
    raw["TextBody"] = None
    raw["MessageID"] = "pm-1"
    email = parse_inbound(raw)
    assert email is not None
    assert email.subject == ""
    assert email.text_body == ""


def test_normalise_message_id() -> None:
    assert normalise_message_id("<a@b>") == "a@b"
    assert normalise_message_id("  a@b ") == "a@b"
    assert normalise_message_id("") == ""
