from dikte.config import Settings, updated
from dikte.textproc import (
    apply_replacements,
    build_prompt,
    clean,
    extract_submit,
    is_hallucination,
    process,
    strip_trailing_hallucination,
)


def test_clean_removes_annotations_and_newlines():
    assert clean(" [BLANK_AUDIO] Merhaba\n dünya (müzik) ♪ ") == "Merhaba dünya"


def test_known_hallucinations_are_dropped():
    for text in ("Altyazı M.K.", "altyazı m.k", "İzlediğiniz için teşekkürler!", "Thanks for watching!", "you", "", "..."):
        assert is_hallucination(text), text


def test_real_short_answers_are_kept():
    for text in ("Teşekkür ederim.", "Tamam.", "Okay.", "Evet", "Thank you."):
        assert not is_hallucination(text), text


def test_trailing_credit_is_stripped_but_content_kept():
    assert strip_trailing_hallucination("Testleri çalıştır. Altyazı M.K.") == "Testleri çalıştır."
    assert strip_trailing_hallucination("Fix the bug. Thanks for watching!") == "Fix the bug."
    assert strip_trailing_hallucination("Altyazı M.K.") == "Altyazı M.K."  # handled by is_hallucination


def test_replacements_respect_word_boundaries_and_case():
    pairs = (("jit hab", "GitHub"), ("yeni satır", "\n"), ("c#", "C#"))
    assert apply_replacements("Jit hab, yeni satır ekle", pairs) == "GitHub, \n ekle"
    assert apply_replacements("jit habit", pairs) == "jit habit"
    assert apply_replacements("use c# here", pairs) == "use C# here"


def test_submit_phrase_is_stripped():
    assert extract_submit("Testleri çalıştır, gönder.", ("gönder",)) == ("Testleri çalıştır", True)
    assert extract_submit("Send it", ("send it",)) == ("", True)
    assert extract_submit("gönderi hazırla", ("gönder",)) == ("gönderi hazırla", False)


def test_process_pipeline():
    settings = updated(Settings(), replacements={"vs kod": "VS Code"}, submit_phrases=["gönder"])
    result = process(" VS kod'u aç ve testleri çalıştır, gönder. Altyazı M.K.", settings)
    assert result.text == "VS Code'u aç ve testleri çalıştır"
    assert result.submit is True
    assert process("Thanks for watching!", settings).text == ""


def test_auto_enter_only_with_text():
    settings = updated(Settings(), auto_enter=True)
    assert process("Merhaba", settings).submit is True
    assert process("", settings).submit is False


def test_prompt_includes_vocabulary_and_is_bounded():
    settings = updated(Settings(), vocabulary=["Kubernetes", "PostgreSQL"])
    prompt = build_prompt(settings, "tr")
    assert prompt.startswith("Tamam") and prompt.endswith("Kubernetes, PostgreSQL.")
    long_settings = updated(Settings(), prompt_tr="kelime " * 130)
    assert len(build_prompt(long_settings, "tr")) <= 600
    assert build_prompt(settings, "en").startswith("Okay")
