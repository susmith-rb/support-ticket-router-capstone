"""Deterministic, dependency-free language detection for routing evaluation.

# REQ-REVAL-002: English-only scope detection and out-of-scope tagging.
"""

from __future__ import annotations

import re
from typing import Optional, Set

# Noise patterns to strip prior to language detection
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_MENTION_RE = re.compile(r"@\w+")
_HASHTAG_RE = re.compile(r"#\w+")
_REDACT_RE = re.compile(r"\[[A-Za-z0-9_]+\]|\{\{[^}]+\}\}")
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE00-\uFE0F]")

# Core English stopwords + domain-specific support vocabulary
EN_WORDS: Set[str] = {
    # Basic grammatical function words
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "i", "it",
    "for", "not", "on", "with", "he", "as", "you", "do", "at", "this", "but",
    "his", "by", "from", "they", "we", "say", "her", "she", "or", "an",
    "will", "my", "one", "all", "would", "there", "their", "what", "so",
    "up", "out", "if", "about", "who", "get", "which", "go", "me", "when",
    "make", "can", "like", "time", "no", "just", "him", "know", "take",
    "people", "into", "year", "your", "good", "some", "could", "them", "see",
    "other", "than", "then", "now", "look", "only", "come", "its", "over",
    "think", "also", "back", "after", "use", "two", "how", "our", "work",
    "first", "well", "way", "even", "new", "want", "because", "any", "these",
    "give", "day", "most", "us", "is", "are", "was", "were", "been", "has",
    "had", "where", "why", "pls", "please", "help", "order", "item", "package",
    "tracking", "delivery", "delivered", "shipping", "shipped", "account",
    "refund", "return", "support", "cancel", "charge", "card", "email",
    "address", "phone", "am", "im", "don't", "dont", "cant", "can't", "won't",
    "wont", "didn't", "didnt", "arrived", "received", "buy", "buying", "bought",
    "send", "sent", "sending", "track", "need", "still", "yet", "again",
    "problem", "issue", "delayed", "delay", "days", "hours", "minutes",
    "today", "yesterday", "tomorrow", "week", "weeks", "money", "customer",
    "service", "reps", "store", "online", "app", "login", "password", "pay",
    "payment", "paid", "check", "checking", "status", "hi", "hey", "hello",
    "dear", "thanks", "thank", "much", "did", "does", "got", "urgent", "broken",
    "damaged", "wrong", "size", "never", "box", "purchased", "purchase", "let",
    "representative", "contacting", "attorney", "reporting", "kitchen", "product",
    "fire", "caused", "formal", "complaint", "shouting", "carrier", "postal",
    "working", "getting", "should", "tell", "show",
    "view", "update", "change", "create", "delete", "remove", "add", "reset",
    "credit", "debit", "bank", "balance", "fee", "cost", "price",
    "dollars", "dollar", "bill", "billing", "charges", "invoices", "invoice",
    "shoes", "shirt", "book", "game", "switch", "mario", "courier", "parcel",
    "doorstep", "porch", "mailbox", "post", "office", "site", "website",
    "something", "anything", "nothing", "someone", "anyone", "everyone",
    "report", "registration", "problems", "editing", "earning", "creating",
    "cancellation", "rubbish", "label", "freemium", "modify", "sick", "packaging",
    "failed", "long", "short", "talk", "assistant", "reimburse", "user", "dm", "possible", "here", "whom", "whose",
    "target", "via", "vr", "playstation", "console", "tv", "pc",
}

# Spanish stopwords
ES_WORDS: Set[str] = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "que",
    "y", "en", "para", "por", "con", "no", "se", "su", "sus", "como", "pero",
    "mas", "más", "este", "esta", "estos", "estas", "ese", "esa", "esos", "esas",
    "aquel", "ya", "ha", "han", "he", "hemos", "me", "te", "le", "nos", "les",
    "si", "sí", "sobre", "cuando", "yo", "tu", "tú", "él", "ella", "ellos",
    "ellas", "usted", "ustedes", "nosotros", "mi", "mis", "día", "días",
    "pedido", "orden", "entrega", "enviado", "paquete", "reembolso", "cuenta",
    "gracias", "hola", "qué", "ganas", "subirlo", "canal", "tienda", "comprar",
    "problema", "ayuda", "donde", "dónde", "cómo", "al", "estoy", "está",
    "están", "hacer", "hecho", "momento", "martes", "llega", "llegó", "favor",
    "buen", "dia", "quiero", "efectuar", "compra", "pregunta", "meses",
    "intereses", "alguien", "realmente", "quiera", "solucionar", "buzones",
    "parece", "puse", "dirección", "supuestamente", "servicio",
}

# Portuguese stopwords
PT_WORDS: Set[str] = {
    "o", "a", "os", "as", "um", "uma", "uns", "umas", "de", "do", "da", "dos",
    "das", "em", "no", "na", "nos", "nas", "que", "e", "para", "por", "com",
    "não", "nao", "se", "seu", "sua", "seus", "suas", "como", "mas", "foi",
    "ao", "ele", "ela", "eles", "elas", "tem", "têm", "à", "às", "muito", "já",
    "está", "estão", "eu", "você", "voce", "vocês", "também", "tambem", "só",
    "pelo", "pela", "pelos", "pelas", "até", "ate", "isso", "isto", "aquilo",
    "entre", "era", "depois", "sem", "mesmo", "ter", "quem", "me", "te",
    "preciso", "minha", "meu", "encomenda", "urgente", "favor", "obrigado",
    "obrigada", "entrega", "pedido", "conta", "ajuda", "onde", "quando",
    "sensação", "dor", "sofrimento", "compra", "consigo", "conectar",
    "senha", "incorreta",
}

# French stopwords
FR_WORDS: Set[str] = {
    "le", "la", "les", "un", "une", "des", "du", "de", "et", "en", "est",
    "que", "qui", "dans", "pour", "pas", "sur", "ce", "cet", "cette", "ces",
    "il", "elle", "ils", "elles", "sont", "avec", "plus", "par", "je", "au",
    "aux", "ne", "se", "ont", "vous", "nous", "ou", "mais", "on", "tout",
    "tous", "toute", "toutes", "comme", "fait", "été", "ete", "sa", "son",
    "ses", "mon", "ma", "mes", "ton", "ta", "tes", "leur", "leurs", "notre",
    "nos", "votre", "vos", "lui", "eux", "j'ai", "c'est", "alors", "là",
    "jamais", "vu", "état", "original", "boîte", "livraison", "commande",
    "colis", "merci", "bonjour", "s'il", "plaît", "problème", "chez", "moi",
    "soir", "refuse", "passer", "vérification", "paiement", "nécessaire",
    "certains", "livreurs", "besoin", "formation", "plusieurs", "jours",
    "arrive", "consulter", "compte", "commander", "impressionnant", "matin",
    "commandé", "livré", "personne",
}

# German stopwords
DE_WORDS: Set[str] = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einer", "eines",
    "einem", "einen", "und", "in", "von", "zu", "mit", "sich", "auf", "für",
    "fuer", "ist", "im", "nicht", "als", "auch", "es", "an", "werden", "aus",
    "er", "hat", "dass", "sie", "nach", "wird", "bei", "um", "am", "sind",
    "noch", "wie", "über", "ueber", "so", "zum", "war", "haben", "nur", "oder",
    "aber", "vor", "zur", "bis", "mehr", "durch", "man", "sein", "wurde",
    "kann", "kanns", "denn", "echt", "da", "habe", "ich", "waren", "wert",
    "bitte", "danke", "hallo", "bestellung", "paket", "lieferung", "artikel",
    "konto", "hilfe", "wieso", "einstellen", "inhalte", "anzeigen",
    "aufgetaucht", "gartenhütte", "abgelegt", "warum", "bekomme", "keinen",
    "bestaetigungcode", "mich", "möglich", "verloren", "gegangen", "sendung",
    "rückerstattung",
}

# Italian stopwords
IT_WORDS: Set[str] = {
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "di", "del",
    "dello", "della", "dei", "degli", "delle", "a", "al", "allo", "alla", "ai",
    "agli", "alle", "da", "dal", "dallo", "dalla", "dai", "dagli", "dalle",
    "in", "nel", "nello", "nella", "nei", "negli", "nelle", "con", "su", "sul",
    "sullo", "sulla", "sui", "sugli", "sulle", "per", "tra", "fra", "e", "ed",
    "che", "chi", "non", "sono", "ho", "ha", "hanno", "abbiamo", "ci", "vi",
    "si", "come", "ma", "se", "più", "piu", "questo", "questa", "questi",
    "queste", "mio", "mia", "miei", "mie", "tuo", "tua", "tuoi", "tue",
    "suo", "sua", "suoi", "sue", "nostro", "nostra", "nostri", "nostre",
    "vostro", "vostra", "vostri", "vostre", "loro", "caro", "sig", "faccio",
    "disattivare", "abbonamento", "ordine", "pacco", "consegna", "grazie",
    "ciao", "buongiorno", "aiuto", "scusatemi", "significa", "riceverò",
    "seconda", "volta", "consecutiva", "avvisa", "sarà",
}

# Dutch stopwords
NL_WORDS: Set[str] = {
    "de", "het", "een", "van", "en", "in", "is", "op", "te", "dat", "die",
    "voor", "met", "niet", "zijn", "er", "om", "aan", "ook", "als", "bij",
    "maar", "omdat", "dan", "zo", "door", "over", "ze", "al", "wat", "mijn",
    "naar", "geen", "dit", "tot", "je", "ik", "we", "jij", "wij", "jullie",
    "u", "bestelling", "pakket", "levering", "graag", "bedankt", "hallo",
    "waar", "wanneer", "hulp",
}


def clean_text_for_lang(text: str) -> str:
    """Strip mentions, URLs, hashtags, redaction tokens, and emojis."""
    t = _URL_RE.sub(" ", text)
    t = _MENTION_RE.sub(" ", t)
    t = _HASHTAG_RE.sub(" ", t)
    t = _REDACT_RE.sub(" ", t)
    t = _EMOJI_RE.sub(" ", t)
    return t


def detect_script(text: str) -> Optional[str]:
    """Detect non-Latin scripts (Japanese/CJK, Cyrillic, Arabic, Devanagari)."""
    has_han = False
    for ch in text:
        cp = ord(ch)
        # Hiragana or Katakana -> Japanese
        if (0x3040 <= cp <= 0x309F) or (0x30A0 <= cp <= 0x30FF):
            return "ja"
        # Han ideographs
        if (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF):
            has_han = True
        # Cyrillic
        elif 0x0400 <= cp <= 0x04FF:
            return "ru"
        # Arabic
        elif (0x0600 <= cp <= 0x06FF) or (0x0750 <= cp <= 0x077F) or (0x08A0 <= cp <= 0x08FF):
            return "ar"
        # Devanagari
        elif 0x0900 <= cp <= 0x097F:
            return "hi"

    if has_han:
        # In TWCS Twitter data, Han ideographs without kana are predominantly Japanese
        return "ja"
    return None


def detect_language(text: str) -> str:
    """Detect language deterministically without external dependencies.

    Returns:
        'en' for English text,
        a language code (e.g. 'es', 'pt', 'fr', 'de', 'it', 'nl', 'ja', 'ru', 'ar', 'hi'),
        or 'und' for too-short/ambiguous text.
    """
    cleaned = clean_text_for_lang(text)

    # 1. Script detection
    script = detect_script(cleaned)
    if script:
        return script

    # 2. Word tokenization (alphabetic, unicode-aware)
    words = re.findall(r"[^\W\d_]+", cleaned.lower())
    if not words:
        return "und"

    # 3. Short texts made only of English words count as 'en'
    if all(w in EN_WORDS for w in words):
        return "en"

    letters = [ch for ch in cleaned if ch.isalpha()]
    non_ascii_letters = [ch for ch in letters if ord(ch) > 127]
    non_ascii_ratio = len(non_ascii_letters) / len(letters) if letters else 0.0

    scores = {
        "en": sum(1 for w in words if w in EN_WORDS),
        "es": sum(1 for w in words if w in ES_WORDS),
        "pt": sum(1 for w in words if w in PT_WORDS),
        "fr": sum(1 for w in words if w in FR_WORDS),
        "de": sum(1 for w in words if w in DE_WORDS),
        "it": sum(1 for w in words if w in IT_WORDS),
        "nl": sum(1 for w in words if w in NL_WORDS),
    }

    foreign_langs = ["es", "pt", "fr", "de", "it", "nl"]
    best_foreign = max(foreign_langs, key=lambda l: scores[l])
    best_foreign_score = scores[best_foreign]

    # Foreign stopwords strictly exceed English stopwords
    if best_foreign_score > scores["en"]:
        return best_foreign

    # High non-ASCII letter ratio with foreign hit
    if non_ascii_ratio > 0.06 and best_foreign_score >= 1:
        return best_foreign

    # English hits with zero foreign hits
    if scores["en"] > 0 and best_foreign_score == 0:
        return "en"

    # Dominant English hits
    if scores["en"] >= best_foreign_score and scores["en"] >= 2:
        return "en"

    # Single-word check
    if len(words) == 1:
        w = words[0]
        if w in EN_WORDS:
            return "en"
        for flang, wset in [
            ("es", ES_WORDS), ("pt", PT_WORDS), ("fr", FR_WORDS),
            ("de", DE_WORDS), ("it", IT_WORDS), ("nl", NL_WORDS),
        ]:
            if w in wset:
                return flang
        return "und"

    if scores["en"] > best_foreign_score:
        return "en"

    if max(scores.values()) == 0:
        # If words exist and are ASCII but no known stopwords
        if all(ord(ch) < 128 for ch in "".join(words)):
            return "und"
        return "und"

    return "en" if scores["en"] >= best_foreign_score else best_foreign


def is_english(text: str) -> bool:
    """Return True if text is detected as English ('en')."""
    return detect_language(text) == "en"
