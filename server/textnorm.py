"""Arabic/English text normalisation helpers shared by the rules and file search."""

from __future__ import annotations

import re

_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_ALEF = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ة": "ه", "ى": "ي", "ؤ": "و", "ئ": "ي"})
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def norm(s: str) -> str:
    """Lower-case, strip Arabic diacritics/tatweel, unify alef/ta-marbuta/ya and digits."""
    s = _DIACRITICS.sub("", s or "")
    s = s.translate(_ALEF).translate(_DIGITS).lower()
    return re.sub(r"\s+", " ", s).strip()


def tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[\s_\-\.]+", norm(s)) if t]


YES_WORDS = {
    "نعم", "ايه", "اي", "ايوه", "ايوا", "اوكي", "تمام", "نفذ", "كمل", "اكمل", "موافق", "صح", "اكيد", "يلا",
    "yes", "yeah", "yep", "ok", "okay", "sure", "go", "confirm", "do it",
}
NO_WORDS = {"لا", "لاء", "لأ", "الغ", "الغي", "الغاء", "وقف", "توقف", "خلاص لا", "no", "nope", "cancel", "stop"}


def yes_no(s: str) -> bool | None:
    t = norm(s)
    if not t:
        return None
    words = set(t.split())
    if t in NO_WORDS or words & NO_WORDS:
        return False
    if t in YES_WORDS or words & YES_WORDS:
        return True
    return None


# ---------------------------------------------------------------------------
# Fuzzy name matching (voice often spells English names in Arabic letters: «تاب» = TAB)
# ---------------------------------------------------------------------------

_TRANSLIT = {
    "ا": "a", "ب": "b", "ت": "t", "ث": "th", "ج": "j", "ح": "h", "خ": "kh", "د": "d", "ذ": "z", "ر": "r",
    "ز": "z", "س": "s", "ش": "sh", "ص": "s", "ض": "d", "ط": "t", "ظ": "z", "ع": "a", "غ": "gh", "ف": "f",
    "ق": "q", "ك": "k", "ل": "l", "م": "m", "ن": "n", "ه": "h", "و": "o", "ي": "i", "ء": "", "ڤ": "v",
    "پ": "p", "چ": "ch", "گ": "g",
}
# letters that sound alike across the two scripts
_SKELETON_MAP = str.maketrans({"q": "k", "c": "k", "g": "j", "v": "f", "p": "b", "z": "s", "x": "ks"})


def translit(s: str) -> str:
    """Arabic letters -> rough Latin spelling (other characters kept)."""
    return "".join(_TRANSLIT.get(ch, ch) for ch in norm(s))


def skeleton(s: str) -> str:
    """Consonant skeleton used to compare spellings: «تاب» and "TAB" both give "tb"."""
    t = re.sub(r"[^a-z0-9]", "", translit(s).replace("th", "t").replace("sh", "s").replace("kh", "k")
               .replace("gh", "g").replace("ph", "f").replace("ck", "k").replace("ch", "k"))
    t = t.translate(_SKELETON_MAP)
    t = re.sub(r"[aeiouywh]", "", t)  # vowels and the weak "h" are spelled too differently to compare
    return re.sub(r"(.)\1+", r"\1", t)


def match_score(query: str, name: str) -> int:
    """0-100: how well a spoken/typed ``query`` matches a file or app ``name`` (extension ignored)."""
    import difflib

    q = norm(query)
    if not q:
        return 0
    full = norm(name)
    stem = re.sub(r"\.[a-z0-9]{1,5}$", "", full)
    if q in (stem, full):
        return 100
    qt = tokens(q)
    if qt and all(t in full for t in qt):
        return 75 + (10 if stem.startswith(qt[0]) else 0) - min(abs(len(stem) - len(q)), 30) // 3
    tq = translit(q)
    if tq and tq != q:
        if tq == stem:
            return 90
        if all(t in stem for t in tq.split()):
            return 65
    sq, ss = skeleton(q), skeleton(stem)
    if len(sq) >= 2 and sq == ss:
        return 62
    if len(sq) >= 3 and any(skeleton(w) == sq for w in re.split(r"[\s_\-.]+", stem) if w):
        return 58
    ratio = difflib.SequenceMatcher(None, tq or q, translit(stem)).ratio()
    if ratio >= 0.78:
        return int(40 + ratio * 20)
    if len(sq) >= 3 and sq in ss:
        return 45
    return 0
