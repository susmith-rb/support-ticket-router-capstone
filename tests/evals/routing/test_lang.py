"""Hermetic unit tests for language detection (evals/routing/lang.py).

# REQ-REVAL-002: English-only scope detection and out-of-scope tagging.
"""

from __future__ import annotations

import pytest

from evals.routing.lang import (
    clean_text_for_lang,
    detect_language,
    detect_script,
    is_english,
)


def test_strip_noise_elements():
    """Verify URLs, mentions, hashtags, redactions, and emojis are stripped."""
    raw = "@support Check out https://example.com/item #order [ORDER] and {{Email}} 😊🔥"
    cleaned = clean_text_for_lang(raw)
    assert "@support" not in cleaned
    assert "https://" not in cleaned
    assert "#order" not in cleaned
    assert "[ORDER]" not in cleaned
    assert "{{Email}}" not in cleaned
    assert "😊" not in cleaned
    assert "Check out" in cleaned


@pytest.mark.parametrize(
    ("text", "expected_script"),
    [
        ("あまぞんさん不良品なんだけど", "ja"),
        ("ところでお世話になっております", "ja"),
        ("Привет, где мой заказ?", "ru"),
        ("أين طلبي؟", "ar"),
        ("मेरा ऑर्डर कहाँ है?", "hi"),
        ("Where is my order?", None),
    ],
)
def test_detect_script(text: str, expected_script: str | None):
    assert detect_script(text) == expected_script


@pytest.mark.parametrize(
    ("text", "expected_lang"),
    [
        # English tweets & standard cases
        ("@user where is my order?!", "en"),
        ("WHERE IS IT", "en"),
        ("pls help", "en"),
        ("why 2 hours ?", "en"),
        ("not working ?", "en"),
        ("can you help ?", "en"),
        ("shipping cost ?", "en"),
        ("modify ordfer [ORDER]", "en"),
        ("I returned item [ORDER] ten days ago and tracking confirms you received it", "en"),
        ("Need Your Help... @user", "en"),
        ("rubbish customer service @user", "en"),

        # Spanish examples (including exact user prompt fixture)
        (
            "@user ya me ha enviado #SuperMarioOdyssey para #NintendoSwitch!!! Qué ganas de subirlo al canal :)",
            "es",
        ),
        ("En un pedido 'prime' q supuestamente llega hoy puse la dirección de...", "es"),
        ("¿Dejar un paquete encima de los buzones? ¿Pero qué clase de servicio es este?", "es"),
        ("hola buen dia quiero efectuar una compra mi pregunta es...", "es"),

        # Portuguese examples
        ("preciso da minha encomenda urgente por favor", "pt"),
        ("Aquela sensação de dor e sofrimento quando você compra um @user e na...", "pt"),
        ("@user, não consigo conectar o Kindle ao Twitter, não é senha incorreta...", "pt"),

        # French examples
        ("@user alors là c'est du jamais vu !", "fr"),
        ("@user bonjour, j'ai une commande prime qui doit arriver chez moi ce soir", "fr"),
        ("Bon, j'avais commandé sur Amazon pour être livré hier entre 18h et 20h. Personne", "fr"),
        ("colis non retiré à la poste et retourné à l'expéditeur : remboursement", "fr"),

        # German examples
        ("@user Kanns denn echt sein. Da habe ich Waren im Wert von 35", "de"),
        ("DPD Paket ist aufgetaucht. Es wurde in der Gartenhütte abgelegt.", "de"),
        ("Warum bekomme ich keinen Bestaetigungcode, @user? Ich kann mich nicht anmelden", "de"),
        ("@user ist es möglich eine verloren gegangen Sendung zu der Rückerstattung", "de"),

        # Italian examples
        ("caro sig. @user come faccio a disattivare l'abbonamento a pr", "it"),
        ("@user Scusatemi, ma significa che lo riceverò?", "it"),
        ("seconda volta consecutiva che #Amazon mi avvisa che il mio ordine sarà", "it"),

        # Japanese examples
        ("あまぞんさん不良品なんだけど💢💢💢💢💢💢 でも交換の仕方みたら面倒くさそう😭", "ja"),
        ("Amazonの店頭到着メールが文字化けしてて読めない", "ja"),
        ("ところでAmazonさん、新品ですかねこれ。シールは破けてるし上の封は開いてるし。", "ja"),

        # Ambiguous / too short
        ("xyzqwerty", "und"),
        ("!!! ??? ...", "und"),
        ("", "und"),
    ],
)
def test_detect_language(text: str, expected_lang: str):
    actual = detect_language(text)
    assert actual == expected_lang, f"Failed for {text!r}: expected {expected_lang}, got {actual}"


def test_is_english_boolean_helper():
    assert is_english("@user where is my order?!") is True
    assert is_english("WHERE IS IT") is True
    assert is_english("pls help") is True
    assert is_english("preciso da minha encomenda urgente por favor") is False
    assert is_english("あまぞんさん不良品なんだけど") is False
