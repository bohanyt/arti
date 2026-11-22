"""Token boundaries for wake calls and the already-fixed origin RAG contract."""
import pytest

from arti_wake import is_arti_wake_call
from arti_vault_rag import enrich_rag_query


@pytest.mark.parametrize("text", [
    "ngerti", "mengerti", "bertemu", "bertanya", "tertawa", "Jakarta", "start",
    "party", "berarti", "aku ngerti kok", "aku tertarik", "tertentu", "bertahan hidup",
    "virtual", "kartun", "aku at home", "rt", "arti_name", "artinya", "artis terkenal",
])
def test_common_words_are_not_wake_calls(text):
    assert not is_arti_wake_call(text)


@pytest.mark.parametrize("text", ["arti", "Arti!", "hai arti", "artii", "artiiii!", "ar ti", "halo—arti!", "(arti)"])
def test_genuine_calls(text):
    assert is_arti_wake_call(text)


@pytest.mark.parametrize("text", ["berarti ada sejak kapan", "mengerti sejarah itu", "Jakarta dibuat kapan"])
def test_origin_rag_does_not_enrich_substrings(text):
    assert enrich_rag_query(text) == text


def test_origin_rag_still_enriches_arti_question():
    assert "arti_origin" in enrich_rag_query("Arti lahir kapan?")
