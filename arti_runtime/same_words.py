"""When two lines of hers say nearly the same, by their own words. No list of topics, places or people.

Only used to choose which older note gives way in a full list, and to show which words her last plans share: nothing
new of hers is ever refused or merged because of it. Function words (closed classes) are left out.
"""
from __future__ import annotations

import re

FUNCTION = frozenset((
    # Indonesian
    'ada adakah adalah agar akan aku apa apakah atau bagaimana bahwa baru belum berapa besok biar bisa bisakah bukan coba '
    'dan dalam dapat dari daripada dengan dia dimana dulu gimana hal hanya hari harus ingin ini itu jadi jika juga kalau '
    'kalian kamu kapan karena kau ke kemana kembali kenapa kita lagi lain lalu lebih lihat maka malam mana mari masih mau '
    'mencoba mengapa mereka mulai mungkin nanti nggak oleh pada pagi para pergi perlu saat saja sama sambil sampai sana '
    'sebuah sedang selesai seperti sesuatu setelah siang siapa sini situ soal sore sudah supaya tak tapi telah tentang '
    'terus tetap tetapi tiap tidak untuk yang '
    # English
    'and are can does for from how not the this what when where which who why with').split())
MIN_SHARED = 2
MIN_SHARE = 0.7


def words(text):
    """The content words of a line in their order: three letters or more and no function word, or a number."""
    found = []
    for word in re.findall(r'[^\W_]+', str(text or '').casefold()):
        if (word.isdigit() or len(word) > 2 and word not in FUNCTION) and word not in found:
            found.append(word)
    return found


def _stem(word, known):
    """'suratnya' is 'surat' where one of these lines says 'surat'; no word is cut otherwise."""
    return word[:-3] if word.endswith('nya') and len(word) > 7 and word[:-3] in known else word


def _joined(sets):
    known = set().union(*sets) if sets else set()
    return [{_stem(w, known) for w in s} for s in sets]


def _marks(text):
    """Words that stand for one thing: numbers, and words written with a capital inside the line."""
    return {w.casefold() for w in re.findall(r'(?<![.!?:]\s)(?<!^)\b[A-Z][^\W_]+', str(text or '').strip())}


def same(a, b, about_a=(), about_b=()):
    """Whether two lines ask nearly the same: the same names, numbers and places, and mostly the same other words."""
    places = {w for tag in (*(about_a or ()), *(about_b or ()))
              for w in re.findall(r'[^\W\d_]+', re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', str(tag)).casefold())}
    one, two = _joined([set(words(a)), set(words(b))])
    marks = _marks(a) | _marks(b) | places | {w for w in one | two if w.isdigit()}
    if {w for w in one if w in marks} != {w for w in two if w in marks}:
        return False
    one, two = one - places, two - places
    shared = len(one & two)
    return bool(one) and (one == two or shared >= MIN_SHARED and shared / min(len(one), len(two)) >= MIN_SHARE)


def common(texts):
    """The content words that stand in every one of these lines, in the order of the first, each once."""
    if not texts:
        return []
    sets = [set(words(t)) for t in texts]
    known = set().union(*sets)
    inside = set.intersection(*_joined(sets))
    return [w for w in dict.fromkeys(_stem(w, known) for w in words(texts[0])) if w in inside]
