from collection_agent.db.models import PolicyStatus
from collection_agent.policy import ChasePolicy
from collection_agent.policy_cards import (
    build_cards,
    clearance_label,
    label_for,
    render_cards,
)

FIELDS = {
    name: {
        "value": getattr(ChasePolicy(), name),
        "quote": f"quote for {name}",
        "source": "document",
    }
    for name in ChasePolicy.model_fields
}


def test_cards_are_in_a_fixed_order_with_the_exact_labels() -> None:
    cards = build_cards(ChasePolicy(), FIELDS, None)

    assert [c.field for c in cards] == [
        "no_response_days",
        "clearance",
        "pre_deadline_buffer_working_days",
        "max_reminders",
        "promise_grace_working_days",
    ]
    assert [c.label for c in cards] == [
        "Follow up after 7 days with no response",
        "Payment must clear by the 15th of the following month",
        "Send a reminder 3 working days before the deadline",
        "Pass to a person after 3 reminders",
        "Allow 2 working days' grace after a missed promise to pay",
    ]
    assert cards[1].value == {"day_of_month": 15, "month_offset": 1}
    assert cards[0].quote == "quote for no_response_days"


def test_clearance_label_for_offsets_0_1_and_2() -> None:
    assert (
        clearance_label(15, 0) == "Payment must clear by the 15th of the month the invoice is sent"
    )
    assert clearance_label(15, 1) == "Payment must clear by the 15th of the following month"
    assert clearance_label(1, 2) == "Payment must clear by the 1st of 2 months after sending"
    assert clearance_label(22, 1).endswith("22nd of the following month")
    assert label_for("clearance", ChasePolicy(clearance_month_offset=2)).endswith(
        "15th of 2 months after sending"
    )


def test_changed_is_judged_against_the_approved_policy() -> None:
    approved = ChasePolicy()
    proposed = ChasePolicy(max_reminders=5, clearance_day_of_month=20)

    cards = {c.field: c for c in build_cards(proposed, FIELDS, approved)}

    assert cards["max_reminders"].changed is True
    assert cards["max_reminders"].current_approved_value == 3
    assert cards["clearance"].changed is True  # either clearance field counts
    assert cards["clearance"].current_approved_value == {"day_of_month": 15, "month_offset": 1}
    assert cards["no_response_days"].changed is False


def test_without_an_approved_policy_nothing_is_changed() -> None:
    for card in build_cards(ChasePolicy(), FIELDS, None):
        assert card.current_approved_value is None
        assert card.changed is False


def test_no_extraction_means_manual_cards_without_quotes() -> None:
    for card in build_cards(ChasePolicy(), None, None):
        assert card.source == "manual"
        assert card.quote is None


def test_quote_is_only_given_for_document_values() -> None:
    fields = {
        **FIELDS,
        "max_reminders": {"value": 3, "quote": None, "source": "fallback_quote_not_found"},
    }
    cards = {c.field: c for c in build_cards(ChasePolicy(), fields, None)}
    assert cards["max_reminders"].source == "fallback_quote_not_found"
    assert cards["max_reminders"].quote is None


def test_the_clearance_card_is_document_only_when_both_parts_are() -> None:
    fields = {
        **FIELDS,
        "clearance_month_offset": {"value": 1, "quote": None, "source": "fallback_not_stated"},
    }
    card = {c.field: c for c in build_cards(ChasePolicy(), fields, None)}["clearance"]
    assert card.source == "fallback_not_stated"
    assert card.quote is None


def test_text_rendering_marks_changes_and_fallbacks() -> None:
    fields = {
        **FIELDS,
        "max_reminders": {"value": 5, "quote": None, "source": "fallback_not_stated"},
    }
    cards = build_cards(ChasePolicy(max_reminders=5), fields, ChasePolicy())
    text = render_cards(2, PolicyStatus.DRAFT, cards, [{"quote": "q", "summary": "be kind"}])

    assert "Policy version 2 (draft)" in text
    assert "Pass to a person after 5 reminders [changed]" in text
    assert "not taken from the document (fallback_not_stated)" in text
    assert 'from the document: "quote for no_response_days"' in text
    assert 'be kind ("q")' in text
