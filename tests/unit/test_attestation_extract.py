from sca.llm import FakeLLM
from sca.tools.attestation_extract import extract_attestation


def test_extract_with_fake_llm():
    fake = FakeLLM(
        json_responses=[
            {
                "as_of_date": "2026-05-01",
                "total_reserves": 61_800_000_000,
                "tokens_outstanding": 61_200_000_000,
                "breakdown": [
                    {"asset_class": "T-bills", "amount": 60_000_000_000}
                ],
                "source_pages": [2, 3],
                "confidence": 0.9,
            }
        ]
    )
    att = extract_attestation(
        symbol="USDC", document_text="<doc>", source_url="u", llm=fake
    )
    assert att.as_of_date == "2026-05-01"
    assert att.total_reserves == 61_800_000_000
    assert att.confidence == 0.9
    assert att.source_pages == [2, 3]
    assert att.breakdown[0].asset_class == "T-bills"


def test_missing_fields_are_safe():
    att = extract_attestation(
        symbol="X", document_text="<doc>", llm=FakeLLM(json_responses=[{}])
    )
    assert att.total_reserves == 0
    assert att.tokens_outstanding == 0
    assert att.confidence == 0.0
    assert att.breakdown == []
