import arti_openrouter


def test_live_chain_fast_only_defaults_are_defined():
    chain = arti_openrouter.openrouter_live_model_chain({})
    assert chain == [
        "nvidia/nemotron-3-super-120b-a12b:free",
        "google/gemma-4-26b-a4b-it:free",
    ]


def test_live_chain_non_fast_empty_overrides_falls_back_to_defined_defaults():
    cfg = {
        "openrouter_live_fast_only": False,
        "openrouter_live_model": "",
        "openrouter_live_fallback_model": "",
        "openrouter_live_last_resort": "",
    }
    chain = arti_openrouter.openrouter_live_model_chain(cfg)
    assert chain == [
        "nvidia/nemotron-3-super-120b-a12b:free",
        "google/gemma-4-26b-a4b-it:free",
    ]


def test_live_chain_non_fast_preserves_explicit_order_without_duplicates():
    cfg = {
        "openrouter_live_fast_only": False,
        "openrouter_live_use_fast_nano": True,
        "openrouter_live_fast_model": "fast/model",
        "openrouter_live_model": "primary/model",
        "openrouter_live_fallback_model": "fallback/model",
        "openrouter_live_last_resort": "primary/model",
    }
    assert arti_openrouter.openrouter_live_model_chain(cfg) == [
        "fast/model",
        "primary/model",
        "fallback/model",
    ]
