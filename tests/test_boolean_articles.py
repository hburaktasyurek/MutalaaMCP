"""Isolated Boolean-query and article-segmentation contract tests."""

from __future__ import annotations

import pytest

from mutalaamcp.domain import boolean
from mutalaamcp.domain.articles import (
    chunk_markdown,
    find_article,
    normalize_article_number,
    segment_articles,
)
from mutalaamcp.domain.boolean import (
    BooleanQueryError,
    parse_boolean_query,
    turkish_fold,
)


def test_boolean_precedence_orders_parentheses_not_and_or() -> None:
    unparenthesized = parse_boolean_query("alpha OR beta AND NOT gamma")
    parenthesized = parse_boolean_query("(alpha OR beta) AND NOT gamma")

    assert unparenthesized.matches("alpha gamma")
    assert not unparenthesized.matches("beta gamma")
    assert unparenthesized.matches("beta only")
    assert not parenthesized.matches("alpha gamma")
    assert parenthesized.matches("alpha only")
    assert parenthesized.match_count("alpha only") == 1


def test_boolean_adjacency_quotes_and_turkish_case_folding() -> None:
    query = parse_boolean_query('"idari işlem" karar NOT yürürlük')

    assert query.matches("İDARİ İŞLEM hakkında karar verilmiştir")
    assert not query.matches("idari işlem karar yürürlükte kalır")
    assert not query.matches("idari karar verilmiştir")
    assert query.literals() == ("idari işlem", "karar", "yürürlük")
    assert turkish_fold("İDARİ IŞIK") == "idari ışık"
    assert parse_boolean_query("IŞIK").matches("ışık")
    assert not parse_boolean_query("IŞIK").matches("işık")


@pytest.mark.parametrize(
    ("query", "text", "expected_count", "expected_literals"),
    (
        ("a AND " * 83 + "ab", "a ab", 167, 84),
        ("z OR " * 99 + "abcde", "abcde", 1, 100),
    ),
)
def test_boolean_evaluates_maximum_length_flat_binary_chains(
    query: str, text: str, expected_count: int, expected_literals: int
) -> None:
    assert len(query) == 500

    parsed = parse_boolean_query(query)

    assert parsed.matches(text)
    assert parsed.match_count(text) == expected_count
    assert len(parsed.literals()) == expected_literals


def test_boolean_evaluates_adversarial_maximum_length_implicit_and_chain() -> None:
    query = "a " * 249 + "ab"
    assert len(query) == 500

    parsed = parse_boolean_query(query)

    assert parsed.matches("a ab")
    assert parsed.match_count("a ab") == 499
    assert parsed.literals() == ("a",) * 249 + ("ab",)


@pytest.mark.parametrize(
    "query",
    (
        "",
        "   ",
        "AND alpha",
        "alpha OR",
        "NOT",
        "()",
        "(alpha",
        "alpha)",
        '"unterminated',
        '""',
        '"bad\\q"',
    ),
)
def test_boolean_rejects_malformed_queries(query: str) -> None:
    with pytest.raises(BooleanQueryError):
        parse_boolean_query(query)


def test_boolean_supports_nesting_just_below_limit() -> None:
    depth = boolean._MAX_NESTING_DEPTH - 1

    nested_not = parse_boolean_query(f"{'NOT ' * depth}alpha")
    nested_parentheses = parse_boolean_query(f"{'(' * depth}alpha{')' * depth}")

    assert nested_not.matches("alpha") is (depth % 2 == 0)
    assert nested_parentheses.matches("alpha")


def test_boolean_rejects_nesting_just_above_limit() -> None:
    depth = boolean._MAX_NESTING_DEPTH + 1

    message = (
        rf"^Sorgu en fazla {boolean._MAX_NESTING_DEPTH} düzey iç içe ifade "
        r"içerebilir; konum: \d+\.$"
    )
    with pytest.raises(BooleanQueryError, match=message):
        parse_boolean_query(f"{'NOT ' * depth}alpha")
    with pytest.raises(BooleanQueryError, match=message):
        parse_boolean_query(f"{'(' * depth}alpha{')' * depth}")


def test_segment_articles_recognizes_published_turkish_heading_forms() -> None:
    markdown = """Önsöz niteliğindeki metin.

# MADDE 1 - Amaç
Birinci maddenin gövdesi.

MADDE 2 / a: Uygulama
İkinci maddenin gövdesi.

EK MADDE 3 — Ek düzenleme
Ek maddenin gövdesi.

GEÇİCİ MADDE 4 - Süre
Geçici maddenin gövdesi.

MÜKERRER MADDE 5/B: Tekrar
Mükerrer maddenin gövdesi.
"""

    articles = segment_articles(markdown)

    assert [article.number for article in articles] == [
        "1",
        "2/A",
        "EK 3",
        "GEÇİCİ 4",
        "MÜKERRER 5/B",
    ]
    assert [article.title for article in articles] == [
        "Amaç",
        "Uygulama",
        "Ek düzenleme",
        "Süre",
        "Tekrar",
    ]
    assert articles[0].markdown.startswith("# MADDE 1 - Amaç")
    assert "MADDE 2 / a" not in articles[0].markdown
    assert find_article(markdown, "madde 2 / a") == articles[1]
    assert find_article(markdown, "ek madde 3") == articles[2]
    assert find_article(markdown, "geçici 4") == articles[3]
    assert find_article(markdown, "mükerrer madde 5 / b") == articles[4]


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (" 1 / a ", "1/A"),
        ("EK MADDE 7", "EK 7"),
        ("geçici 8/b", "GEÇİCİ 8/B"),
        ("Mükerrer Madde 9", "MÜKERRER 9"),
    ),
)
def test_article_number_normalization_preserves_published_labels(
    value: str, expected: str
) -> None:
    assert normalize_article_number(value) == expected


def test_chunk_markdown_reconstructs_complete_article_document() -> None:
    markdown = (
        "# MADDE 1 - Amaç\n"
        "Birinci maddenin gövdesi.\n\n"
        "MADDE 2 - Uygulama\n"
        "İkinci maddenin gövdesi."
    )

    chunks = chunk_markdown(markdown, max_chars=10_000)

    assert "\n\n".join(chunks) == markdown


def test_chunk_markdown_retains_title_and_preamble_before_first_article() -> None:
    preamble = "# Örnek Kanun\n\nBu Kanunun kapsamını açıklayan önsöz metni.  "
    article = "MADDE 1 - Amaç\nKanunun amacı bu maddede düzenlenir.  "
    markdown = f"{preamble}\n\n{article}\n"

    chunks = chunk_markdown(markdown, max_chars=len(preamble))

    assert chunks[0] == preamble
    assert "\n\n".join(chunks) == markdown


def test_chunk_markdown_keeps_article_boundaries_intact() -> None:
    first_article = "MADDE 1 - Amaç\nBirinci maddenin gövdesi."
    second_article = "MADDE 2 - Uygulama\nİkinci maddenin gövdesi."

    chunks = chunk_markdown(
        f"{first_article}\n\n{second_article}", max_chars=len(first_article)
    )

    assert chunks == (first_article, second_article)


def test_chunk_markdown_preserves_page_order_when_splitting_long_article() -> None:
    max_chars = 100
    first_page = "MADDE 1 - Uygulama\n" + "Birinci paragrafın metni. " * 3
    second_page = "İkinci paragrafın metni. " * 3
    third_page = "Üçüncü paragrafın metni. " * 3
    markdown = f"{first_page}\n\n{second_page}\n\n{third_page}"

    pages = chunk_markdown(markdown, max_chars=max_chars)

    assert pages == (first_page, second_page, third_page)
    assert "\n\n".join(pages) == markdown


def test_chunk_markdown_reconstructs_multiple_articles_with_one_split_article() -> None:
    max_chars = 120
    first_article = (
        "MADDE 1 - Uygulama\n"
        + "Birinci paragrafın metni. " * 3
        + "\n\n"
        + "İkinci paragrafın metni. " * 3
    )
    second_article = "MADDE 2 - Sonuç\nKısa ikinci madde."
    markdown = f"{first_article}\n\n{second_article}"

    chunks = chunk_markdown(markdown, max_chars=max_chars)

    assert "\n\n".join(chunks) == markdown
    assert all(len(chunk) <= max_chars for chunk in chunks)


def test_article_index_excludes_appended_laws_tables_and_footnotes() -> None:
    markdown = """# BİRİNCİ KISIM
# Genel Hükümler
MADDE 1- Ana hüküm.[[1]](#_ftn1)

GEÇİCİ MADDE 2- Ana kira hükmü.

6098 SAYILI KANUNA İŞLENEMEYEN HÜKÜMLER

GEÇİCİ MADDE 2- Başka kanunun geçici hükmü.

6098 SAYILI KANUNA EK VE DEĞİŞİKLİK GETİREN MEVZUATIN VEYA
ANAYASA MAHKEMESİ KARARLARININ YÜRÜRLÜĞE GİRİŞ
TARİHLERİNİ GÖSTERİR TABLO

Geçici Madde 2
15/7/2023

---

[[1]](#_ftnref1) Değişiklik açıklaması.

MADDE 9- Dipnotta alıntılanan hüküm.
"""
    articles = segment_articles(markdown)
    assert [a.number for a in articles] == ["1", "GEÇİCİ 2"]
    assert articles[1].markdown == "GEÇİCİ MADDE 2- Ana kira hükmü."
    assert find_article(markdown, "9") is None
    assert "Değişiklik açıklaması" in find_article(markdown, "1").markdown
    assert "MADDE 9" in find_article(markdown, "1").markdown
    # Full document delivery still preserves appendices and footnotes.
    assert "Başka kanunun" in "\n\n".join(chunk_markdown(markdown))


def test_duplicate_main_article_labels_fail_instead_of_selecting_first() -> None:
    from mutalaamcp.domain.articles import AmbiguousArticleError

    markdown = "MADDE 1- Birinci metin.\n\nMADDE 1- Farklı metin."
    with pytest.raises(AmbiguousArticleError, match="tek madde güvenle"):
        find_article(markdown, "1")


def test_soft_hyphen_in_article_label_is_recognized_without_changing_text() -> None:
    markdown = (
        "MADDE 463- Önceki.\n\nMADDE 464\u00ad- Korunacak metin.\n\nMADDE 465- Sonraki."
    )
    assert [a.number for a in segment_articles(markdown)] == ["463", "464", "465"]
    assert find_article(markdown, "464").markdown == "MADDE 464\u00ad- Korunacak metin."


def test_outline_reads_explicit_divisions_and_excludes_appendix() -> None:
    from mutalaamcp.domain.articles import outline_from_markdown

    markdown = """# BİRİNCİ KISIM
# Genel Hükümler
# BİRİNCİ BÖLÜM
# Borç İlişkisinin Kaynakları
# BİRİNCİ AYIRIM
# Sözleşmeden Doğan Borç İlişkileri
1. Genel olarak
MADDE 1- Birinci hüküm.

İKİNCİ KISIM
Özel Borç İlişkileri
DÖRDÜNCÜ BÖLÜM
Kira Sözleşmesi
BİRİNCİ AYIRIM
Genel Hükümler
A. Tanımı
MADDE 299- İkinci hüküm.

6098 SAYILI KANUNA İŞLENEMEYEN HÜKÜMLER
GEÇİCİ MADDE 2- Ek kanun.
"""
    nodes = outline_from_markdown(markdown)
    assert len(nodes) == 2
    assert nodes[0]["title"] == "BİRİNCİ KISIM — Genel Hükümler"
    section = nodes[1]["children"][0]
    assert section["title"] == "DÖRDÜNCÜ BÖLÜM — Kira Sözleşmesi"
    assert section["children"][0]["children"] == [
        {"article_number": "299", "title": "Madde 299 — A. Tanımı", "children": []}
    ]
    assert outline_from_markdown("MADDE 1- Başlıksız belge.") == []


def test_following_division_is_not_part_of_previous_article_search_text() -> None:
    markdown = """MADDE 298- Bağışlama hükmü.

DÖRDÜNCÜ BÖLÜM
Kira Sözleşmesi
BİRİNCİ AYIRIM
Genel Hükümler
A. Tanımı
MADDE 299- Kira sözleşmesinin tanımı.

## B. Süresi
MADDE 300- Süre hükmü.
"""
    articles = segment_articles(markdown)
    assert articles[0].markdown == "MADDE 298- Bağışlama hükmü."
    assert articles[1].markdown == "MADDE 299- Kira sözleşmesinin tanımı."
    assert [
        a.number for a in articles if parse_boolean_query("kira").matches(a.markdown)
    ] == ["299"]
    assert "Kira Sözleşmesi" in "\n\n".join(chunk_markdown(markdown))
