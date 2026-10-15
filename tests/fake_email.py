"""In-memory EmailSender. Never touches the network."""

from collection_agent.email_sender import EmailSendError, OutgoingEmail, SentEmail


class FakeEmailSender:
    def __init__(self) -> None:
        self.sent: list[OutgoingEmail] = []
        self.fail_with: EmailSendError | None = None

    def send(self, message: OutgoingEmail) -> SentEmail:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(message)
        return SentEmail(provider_message_id=f"fake-msg-{len(self.sent)}")
