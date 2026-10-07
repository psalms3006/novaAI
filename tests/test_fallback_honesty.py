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


# -- a failed tool is not a success (2026-10 audit) --------------------------

def test_an_error_string_is_not_a_successful_tool_run():
    from desk import chat
    assert chat._succeeded("Refused: file_write is turned off.") is False
    assert chat._succeeded("Action cancelled. I won't delete the folder.") is False
    assert chat._succeeded("") is False
    assert chat._succeeded("Saved C:/Users/x/report.docx") is True


def test_claiming_success_after_every_tool_failed_is_flagged():
    from desk import chat
    out = chat._unbacked_claim_note(
        "I've deleted the folder.",
        [{"name": "file_controller", "label": "Files", "ok": False}])
    assert "did not actually succeed" in out and "Files" in out


def test_a_real_success_is_left_alone():
    from desk import chat
    text = "I've saved the report."
    assert chat._unbacked_claim_note(
        text, [{"name": "generate_document", "label": "Document", "ok": True}]) == text
