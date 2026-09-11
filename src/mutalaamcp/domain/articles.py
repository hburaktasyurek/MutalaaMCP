"""Article-aware segmentation, lookup, and Markdown chunking."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

_TURKISH_LETTERS = "A-Za-zÇĞİÖŞÜçğıöşü"
_TURKISH_CASE_TRANSLATION: dict[str, str | int | None] = {
    "I": "ı",
    "İ": "i",
}
_ARTICLE_HEADING = re.compile(
    rf"""^
    [ \t]*(?:\#{{1,6}}(?=[ \t]))?[ \t]*
    (?P<prefix>MADDE|EK(?:\s+MADDE)?|GEÇİCİ(?:\s+MADDE)?|MÜKERRER(?:\s+MADDE)?)
    \s*(?P<number>\d+(?:\s*/\s*[{_TURKISH_LETTERS}]+)?)
    (?=\s|[.\-–—:(\u00ad]|$)(?P<rest>.*)$
    """,
    re.IGNORECASE | re.VERBOSE,
)
_ANNEX_HEADING = re.compile(
    r"^\d+\s+SAYILI\s+KANUN\S*\s+(?:İŞLENEMEYEN\s+HÜKÜMLER|EK\s+VE\s+DEĞİŞİKLİK\s+GETİREN)",
    re.IGNORECASE,
)
_FOOTNOTE = re.compile(r"^\[\[(\d+)\]\]\(#_ftnref\d+\)")
_DIVISION = re.compile(
    r"^(?:[A-ZÇĞİÖŞÜ]+(?:\s+[A-ZÇĞİÖŞÜ]+)*|[IVXLCDM]+|\d+)\s+(KİTAP|KISIM|BÖLÜM|AYIRIM)$"
)


class AmbiguousArticleError(ValueError):
    """More than one main-text section has the requested published label."""


def _plain_heading(line: str) -> str:
    return line.strip().lstrip("#").strip().strip("*").strip()


def _main_lines(markdown: str) -> list[str]:
    """Bound the article index, leaving the full document entirely unchanged."""
    lines = markdown.splitlines(keepends=True)
    for index, line in enumerate(lines):
        text = _plain_heading(line)
        if _ANNEX_HEADING.match(text) or _FOOTNOTE.match(text):
            return lines[:index]
    return lines


def _caption_lines(lines: list[str]) -> set[int]:
    """Identify source headings that belong to the following article, not prose."""
    captions: set[int] = set()
    for index, line in enumerate(lines):
        text = _plain_heading(line)
        if not text or _ARTICLE_HEADING.match(line):
            continue
        division = _DIVISION.fullmatch(text)
        if division:
            captions.add(index)
            for following in range(index + 1, len(lines)):
                label = _plain_heading(lines[following])
                if not label:
                    continue
                if (
                    len(label) <= 160
                    and not label.endswith((".", ";", ":"))
                    and not _ARTICLE_HEADING.match(lines[following])
                ):
                    captions.add(following)
                break
        elif (
            len(text) <= 160
            and not text.endswith((".", ";", ":"))
            and (
                line.lstrip().startswith("#")
                or re.match(r"^(?:[A-Za-zÇĞİÖŞÜçğıöşü]|[IVXLCDM]+|\d+)\.\s+\S", text)
                or text in {"Yürürlük", "Yürütme"}
            )
        ):
            captions.add(index)
    return captions


def _preamble_start(lines: list[str], article_start: int, captions: set[int]) -> int:
    first = article_start
    for index in range(article_start - 1, -1, -1):
        if not _plain_heading(lines[index]):
            continue
        if index not in captions:
            break
        first = index
    return first


@dataclass(frozen=True, slots=True)
class Article:
    """A published article section parsed from normalized legislation Markdown."""

    number: str
    title: str | None
    markdown: str


@dataclass(frozen=True, slots=True)
class OutlineArticle:
    """An upstream tree leaf, optionally carrying Bedesten's MADDE identifier."""

    number: str
    title: str | None
    article_id: str | None


def normalize_article_number(value: str) -> str:
    """Normalize request and upstream labels to the public article-number form."""
    if not isinstance(value, str):
        raise TypeError("Madde numarası str türünde olmalıdır.")
    compact = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    compact = re.sub(r"\s*/\s*", "/", compact)
    compact = re.sub(r"\bMADDE\b", "", compact, flags=re.IGNORECASE)
    compact = " ".join(compact.split())
    match = re.fullmatch(
        rf"(?:(EK|GEÇİCİ|MÜKERRER)\s+)?(\d+(?:/[{_TURKISH_LETTERS}]+)?)",
        compact,
        re.IGNORECASE,
    )
    if match is None:
        raise ValueError(
            "Madde numarası, yayımlanmış bir MADDE, EK, GEÇİCİ veya MÜKERRER "
            "etiketi olmalıdır."
        )
    prefix, number = match.groups()
    number = number.upper()
    if prefix is None:
        return number
    prefix_key = _article_fold(prefix)
    canonical_prefixes = {
        "ek": "EK",
        "geçici": "GEÇİCİ",
        "mükerrer": "MÜKERRER",
    }
    return f"{canonical_prefixes[prefix_key]} {number}"


def segment_articles(markdown: str) -> tuple[Article, ...]:
    """Split converted legislation Markdown at actual MADDE-style headings.

    The heading itself stays in each article so a direct article response is
    intelligible and preserves the source text verbatim. Prose before the first
    article is intentionally not synthesized into an article.
    """
    if not isinstance(markdown, str):
        raise TypeError("markdown str türünde olmalıdır.")
    lines = _main_lines(markdown)
    starts = _article_starts(lines)
    captions = _caption_lines(lines)

    articles: list[Article] = []
    seen: set[str] = set()
    for position, (start, number, title) in enumerate(starts):
        if number in seen:
            raise AmbiguousArticleError(
                f"Ana metinde {number} etiketi birden fazla kez bulundu; "
                "tek madde güvenle seçilemiyor. Tam belgeyi inceleyin."
            )
        seen.add(number)
        end = (
            _preamble_start(lines, starts[position + 1][0], captions)
            if position + 1 < len(starts)
            else len(lines)
        )
        body = "".join(lines[start:end]).strip()
        if body:
            articles.append(Article(number=number, title=title, markdown=body))
    return tuple(articles)


def find_article(markdown: str, article_number: str) -> Article | None:
    """Return the requested article by canonical published label."""
    requested = normalize_article_number(article_number)
    article = next(
        (
            article
            for article in segment_articles(markdown)
            if article.number == requested
        ),
        None,
    )
    if article is None:
        return None
    # Keep referenced editorial notes in direct article retrieval, but do not
    # index the appendix/footnote area as additional articles in body searches.
    refs = set(re.findall(r"\]\(#_ftn(\d+)\)", article.markdown))
    notes: list[str] = []
    lines = markdown.splitlines(keepends=True)
    starts = [
        (i, m.group(1))
        for i, line in enumerate(lines)
        if (m := _FOOTNOTE.match(line.strip()))
    ]
    for pos, (start, label) in enumerate(starts):
        if label in refs:
            end = starts[pos + 1][0] if pos + 1 < len(starts) else len(lines)
            notes.append("".join(lines[start:end]).strip())
    if notes:
        return Article(
            article.number,
            article.title,
            article.markdown + "\n\n" + "\n\n".join(notes),
        )
    return article


def chunk_markdown(markdown: str, *, max_chars: int = 12_000) -> tuple[str, ...]:
    """Chunk normalized text without cutting an article or dropping its preamble.

    Long sections are split only on paragraph boundaries. A paragraph longer
    than ``max_chars`` remains intact because fixed-character slicing is
    expressly not a document contract boundary. Joining chunks with a blank
    line reconstructs the normalized source order, including the title and
    explanatory prose before the first MADDE heading.
    """
    if not isinstance(markdown, str):
        raise TypeError("markdown str türünde olmalıdır.")
    if max_chars < 1:
        raise ValueError("max_chars en az 1 olmalıdır.")
    sections = _chunk_sections(markdown)
    chunks: list[str] = []
    current = ""
    for section in sections:
        if not section:
            continue
        candidate = section if not current else f"{current}\n\n{section}"
        if len(section) <= max_chars:
            if current and len(candidate) > max_chars:
                chunks.append(current)
                current = section
            else:
                current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""
        for paragraph in (piece for piece in section.split("\n\n") if piece):
            candidate = paragraph if not current else f"{current}\n\n{paragraph}"
            if current and len(candidate) > max_chars:
                chunks.append(current)
                current = paragraph
            else:
                current = candidate
        if current:
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    return tuple(chunks) or ("",)


def _article_starts(lines: list[str]) -> list[tuple[int, str, str | None]]:
    starts: list[tuple[int, str, str | None]] = []
    for index, line in enumerate(lines):
        match = _ARTICLE_HEADING.match(line)
        if match is None:
            continue
        try:
            number = _heading_number(match.group("prefix"), match.group("number"))
        except ValueError:
            continue
        starts.append((index, number, _heading_title(match.group("rest"))))
    return starts


def _chunk_sections(markdown: str) -> list[str]:
    lines = markdown.splitlines(keepends=True)
    starts = _article_starts(lines)
    if not starts:
        return [markdown]

    sections: list[str] = []
    current = "".join(lines[: starts[0][0]])
    for position, (start, _, _) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        section = "".join(lines[start:end])
        if current.endswith("\n\n"):
            previous = current[:-2]
            if previous:
                sections.append(previous)
            current = section
        else:
            current += section
    if current:
        sections.append(current)
    return sections


def extract_outline_articles(payload: object) -> tuple[OutlineArticle, ...]:
    """Flatten heterogeneous Bedesten tree payloads into article lookup leaves."""
    output: list[OutlineArticle] = []
    seen: set[tuple[str, str | None]] = set()
    for node in _walk_nodes(payload):
        number = _read_string(
            node, "maddeNo", "maddeNumarasi", "articleNumber", "article_no", "numara"
        )
        if number is None:
            label = _read_string(node, "baslik", "title", "adi", "name")
            if label:
                heading = _ARTICLE_HEADING.match(label)
                if heading is not None:
                    number = _heading_number(
                        heading.group("prefix"), heading.group("number")
                    )
        if number is None:
            continue
        try:
            normalized = normalize_article_number(number)
        except ValueError:
            continue
        article_id = _read_string(node, "maddeId", "articleId", "id", "documentId")
        title = _read_string(node, "maddeBaslik", "title", "baslik", "name")
        key = (normalized, article_id)
        if key not in seen:
            output.append(OutlineArticle(normalized, title, article_id))
            seen.add(key)
    return tuple(output)


def normalize_outline(payload: object) -> list[dict[str, object]]:
    """Return the public recursive outline shape, preserving only safe fields."""
    roots = _root_nodes(payload)
    return [_normalize_node(node) for node in roots if _node_title(node)]


def outline_needs_titles(payload: object) -> bool:
    """Only enrich an upstream outline that actually advertises placeholders."""
    return any(
        re.fullmatch(r"Madde No\s*:\s*.+", _node_title(node), re.IGNORECASE)
        for node in _walk_nodes(payload)
    )


def outline_from_markdown(markdown: str) -> list[dict[str, object]]:
    """Read explicit Turkish division headings; never infer numeric ranges."""
    lines = _main_lines(markdown)
    # Reject ambiguous labels before constructing a plausible-looking tree.
    articles = {a.number: a for a in segment_articles(markdown)}
    ranks = {"KİTAP": 0, "KISIM": 1, "BÖLÜM": 2, "AYIRIM": 3}
    roots: list[dict[str, object]] = []
    stack: list[tuple[int, list[dict[str, object]]]] = [(-1, roots)]
    has_division = False
    previous = ""
    skip: set[int] = set()
    for index, line in enumerate(lines):
        text = _plain_heading(line)
        if not text or index in skip:
            continue
        division = _DIVISION.fullmatch(text)
        if division:
            rank = ranks[division.group(1)]
            title = text
            for following in range(index + 1, len(lines)):
                caption = _plain_heading(lines[following])
                if not caption:
                    continue
                # Captions must be standalone short lines, not article prose or
                # the next hierarchy marker (A., I., 1. etc.).
                if (
                    len(caption) <= 160
                    and not caption.endswith((".", ";", ":"))
                    and not _DIVISION.fullmatch(caption)
                    and not _ARTICLE_HEADING.match(caption)
                    and not re.match(r"^(?:[A-ZÇĞİÖŞÜ]|[IVXLCDM]+|\d+)\.", caption)
                ):
                    title += " — " + caption
                    skip.add(following)
                break
            while stack[-1][0] >= rank:
                stack.pop()
            children: list[dict[str, object]] = []
            stack[-1][1].append(
                {"article_number": None, "title": title, "children": children}
            )
            stack.append((rank, children))
            has_division = True
            previous = ""
            continue
        heading = _ARTICLE_HEADING.match(line)
        if heading:
            number = _heading_number(heading.group("prefix"), heading.group("number"))
            article = articles[number]
            label = f"Madde {number}"
            # Preserve a source heading immediately preceding an article.
            if (
                previous
                and len(previous) <= 160
                and not previous.endswith((".", ";", ":"))
            ):
                label += " — " + previous
            elif article.title:
                label += " — " + article.title
            stack[-1][1].append(
                {"article_number": number, "title": label, "children": []}
            )
            previous = ""
        else:
            previous = text
    return roots if has_division else []


def _root_nodes(payload: object) -> list[Mapping[str, object]]:
    if isinstance(payload, Mapping):
        for key in ("data", "items", "nodes", "children", "content", "result"):
            child = payload.get(key)
            if child is not None:
                roots = _root_nodes(child)
                if roots:
                    return roots
        return [payload]
    if isinstance(payload, list):
        return [node for node in payload if isinstance(node, Mapping)]
    return []


def _normalize_node(node: Mapping[str, object]) -> dict[str, object]:
    title = _node_title(node)
    article_number = _node_article_number(node)
    children = _child_nodes(node)
    return {
        "article_number": article_number,
        "title": title,
        "children": [
            _normalize_node(child) for child in children if _node_title(child)
        ],
    }


def _node_title(node: Mapping[str, object]) -> str:
    title = _read_string(node, "title", "baslik", "adi", "name", "maddeBaslik")
    if title:
        return title
    number = _node_article_number(node)
    return number or "Başlıksız"


def _node_article_number(node: Mapping[str, object]) -> str | None:
    value = _read_string(
        node, "maddeNo", "maddeNumarasi", "articleNumber", "article_no", "numara"
    )
    if value is None:
        return None
    try:
        return normalize_article_number(value)
    except ValueError:
        return None


def _child_nodes(node: Mapping[str, object]) -> list[Mapping[str, object]]:
    for key in ("children", "altMaddeler", "altBasliklar", "items", "nodes"):
        value = node.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    return []


def _walk_nodes(payload: object) -> Iterable[Mapping[str, object]]:
    if isinstance(payload, Mapping):
        yield payload
        for value in payload.values():
            yield from _walk_nodes(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _walk_nodes(value)


def _read_string(node: Mapping[str, object], *names: str) -> str | None:
    for name in names:
        value = node.get(name)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return None


def _heading_number(prefix: str, number: str) -> str:
    compact_prefix = _article_fold(unicodedata.normalize("NFKC", prefix))
    if compact_prefix.startswith("ek"):
        label = f"EK {number}"
    elif compact_prefix.startswith("geçici"):
        label = f"GEÇİCİ {number}"
    elif compact_prefix.startswith("mükerrer"):
        label = f"MÜKERRER {number}"
    else:
        label = number
    return normalize_article_number(label)


def _article_fold(value: str) -> str:
    return (
        value.translate(str.maketrans(_TURKISH_CASE_TRANSLATION))
        .casefold()
        .replace("ı", "i")
    )


def _heading_title(rest: str) -> str | None:
    trimmed = rest.strip().lstrip(".-–—:").strip()
    if not trimmed or trimmed.startswith("("):
        return None
    # A prose-heavy line following the dash is article text, not a title.
    if len(trimmed) > 180 or trimmed.endswith((".", ";")):
        return None
    return trimmed
