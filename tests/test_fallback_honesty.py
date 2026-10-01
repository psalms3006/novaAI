"""2026-10-01: with both flash models out of quota, chat fell to tinyllama,
which cannot call tools, and it answered "Yes, I did change the file" about a
file nothing touched."""
from desk import chat
from nova_intelligence.gemini_provider import GeminiProvider


def test_a_claimed_change_with_no_tool_run_is_called_out():
    out = chat._unbacked_claim_note("Yes, I did change the file. The table was added.", [])
    assert "nothing on your computer was actually changed" in out
    out = chat._unbacked_claim_note("I've created the report and saved it to Documents.",
                                    [{"name": "generate_document", "ok": False}])
    assert "nothing on your computer" in out


def test_real_work_and_plain_answers_are_left_alone():
    done = [{"name": "file_controller", "ok": True}]
    assert chat._unbacked_claim_note("I saved it to Documents.", done) == "I saved it to Documents."
    plain = "Dropdowns should animate in 150-250ms; I recommend ease-out."
    assert chat._unbacked_claim_note(plain, []) == plain
    assert chat._unbacked_claim_note("I did not change the file.", []) == "I did not change the file."


def test_flash_lite_is_in_the_cloud_fallback_chain():
    assert GeminiProvider.FALLBACK_MODELS[0] == "gemini-2.5-flash"
    assert "gemini-flash-lite-latest" in GeminiProvider.FALLBACK_MODELS
