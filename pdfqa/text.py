"""Tokenizing, a light stemmer and quote normalization shared by the index and the verifier."""

from __future__ import annotations

import re
import unicodedata

STOPWORDS = frozenset("""
a an and are as at be been but by can could did do does for from had has have how i if in into is it
its may might must not of on or our shall should so such than that the their them then there these they
this those to was we were what when where which while who whom why will with would you your about after
before any all also each per other more most only over under up out own same both very just via
many much
""".split())

_TOKEN = re.compile(r"[^\W_]+(?:[.,][0-9]+)*", re.UNICODE)


def normalize_number(token: str) -> str:
    """'1,250.00' -> '1250.00', '41.2' stays; thousands separators never split a number."""
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", token):
        return token.replace(",", "")
    return token.rstrip(".,")


def stem(word: str) -> str:
    """A small suffix stripper: enough to match plural, past and -ing forms.

    A final "e" is dropped as well, so "rate", "rated", "rates" and "rating" all
    become "rat". Words of four letters or fewer keep their suffixes ("used").
    """
    if len(word) <= 3 or not word.isalpha():
        return word
    for _ in range(2):  # "bookings" -> "booking" -> "book"
        before = word
        if len(word) > 4:
            for suffix, keep in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
                if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                    if suffix == "s" and word.endswith(("ss", "us", "is")):
                        break
                    if suffix == "es" and not word.endswith(("ches", "shes", "xes", "sses", "zes")):
                        continue
                    word = word[: len(word) - len(suffix)] + keep
                    break
        if word == before:
            break
    if word.endswith("e") and len(word) > 3:
        word = word[:-1]
    return word


def tokens(text: str, keep_stopwords: bool = False) -> list[str]:
    text = unicodedata.normalize("NFKC", text).lower()
    out = []
    for raw in _TOKEN.findall(text):
        tok = normalize_number(raw)
        if not tok or (not keep_stopwords and tok in STOPWORDS):
            continue
        out.append(stem(tok))
    return out


def normalize_quote(text: str) -> str:
    """Lowercase words only: ignores line breaks, hyphenation, quote styles and punctuation."""
    text = unicodedata.normalize("NFKC", text).replace("­", "")
    text = re.sub(r"(?<=[^\W\d_])-\s*\n\s*(?=[^\W\d_])", "", text)  # words hyphenated across lines
    return " ".join(re.findall(r"\w+", text.lower()))


_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9Ѐ-Я])")


def sentences(text: str) -> list[str]:
    """Split prose into sentences without breaking '4.2', 'e.g.' or 'No. 5'."""
    parts = []
    for line in text.split("\n"):
        for piece in _SENTENCE.split(line):
            piece = piece.strip()
            if not piece:
                continue
            if parts and re.search(r"\b(e\.g|i\.e|No|Art|Sec|vs|approx|Mr|Ms|Dr|Ltd|Inc)\.$", parts[-1]):
                parts[-1] += " " + piece
            else:
                parts.append(piece)
    return parts
