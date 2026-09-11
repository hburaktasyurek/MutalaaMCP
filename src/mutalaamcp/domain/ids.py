"""Strict namespaced document identifiers.

Canonical forms:

- ``bedesten:<opaque>``
- ``mevzuat:<opaque>``
- ``anayasa:nd:<year>:<sequence>``
- ``anayasa:bb:<year>:<sequence>``

Unknown namespaces and URLs are rejected. Citation-shaped legislation
ids are not mintable: ``mevzuat:`` carries only an opaque Bedesten id.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, TypeGuard

_OPAQUE_NAMESPACES = frozenset({"bedesten", "mevzuat"})
_ANAYASA_KINDS = frozenset({"nd", "bb"})
_OPAQUE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
)
ANAYASA_SEQUENCE_MAX_DIGITS: Final = 18
_MAX_ANAYASA_SEQUENCE: Final = 10**ANAYASA_SEQUENCE_MAX_DIGITS - 1


class InvalidDocumentId(ValueError):
    """Raised when a value is not a supported namespaced document id."""

    def __init__(self, value: object, reason: str) -> None:
        self.value = value
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class BedestenId:
    opaque: str

    def __post_init__(self) -> None:
        _require_opaque(self.opaque)

    def format(self) -> str:
        return f"bedesten:{self.opaque}"


@dataclass(frozen=True, slots=True)
class MevzuatId:
    opaque: str

    def __post_init__(self) -> None:
        _require_opaque(self.opaque)

    def format(self) -> str:
        return f"mevzuat:{self.opaque}"


@dataclass(frozen=True, slots=True)
class AnayasaId:
    kind: Literal["nd", "bb"]
    year: int
    sequence: int

    def __post_init__(self) -> None:
        if self.kind not in _ANAYASA_KINDS:
            raise InvalidDocumentId(self.kind, "anayasa türü 'nd' veya 'bb' olmalıdır")
        if not (1000 <= self.year <= 9999):
            raise InvalidDocumentId(
                self.year, "anayasa yılı 4 basamaklı bir tam sayı olmalıdır"
            )
        if not 1 <= self.sequence <= _MAX_ANAYASA_SEQUENCE:
            raise InvalidDocumentId(
                self.sequence,
                f"anayasa sıra numarası 1 ile {_MAX_ANAYASA_SEQUENCE} arasında olmalıdır",
            )

    def format(self) -> str:
        return f"anayasa:{self.kind}:{self.year}:{self.sequence}"

    def route(self) -> str:
        """Normalized AYM path: ``/ND/YYYY/N`` or ``/BB/YYYY/N``."""
        return f"/{self.kind.upper()}/{self.year}/{self.sequence}"


DocumentId = BedestenId | MevzuatId | AnayasaId


def parse_document_id(value: str) -> DocumentId:
    """Parse a canonical namespaced document id.

    Exact round-trip: ``format_document_id(parse_document_id(s)) == s``
    for every accepted ``s``.
    """
    if not isinstance(value, str):
        raise InvalidDocumentId(value, "belge kimliği bir dizge olmalıdır")
    if not value or any(ch.isspace() for ch in value):
        raise InvalidDocumentId(
            value, "belge kimliği boş olmamalı ve boşluk içermemelidir"
        )
    if _is_url(value):
        raise InvalidDocumentId(value, "belge kimliği bir URL olmamalıdır")

    parts = value.split(":")
    if len(parts) == 2:
        namespace, opaque = parts
        if namespace == "bedesten":
            _require_opaque(opaque, value)
            return BedestenId(opaque)
        if namespace == "mevzuat":
            _require_opaque(opaque, value)
            return MevzuatId(opaque)
        raise InvalidDocumentId(
            value, f"bilinmeyen belge kimliği ad alanı '{namespace}'"
        )

    if len(parts) == 4 and parts[0] == "anayasa":
        _, kind, year_text, sequence_text = parts
        if _is_anayasa_kind(kind):
            year = _parse_anayasa_year(year_text, value)
            sequence = _parse_anayasa_sequence(sequence_text, value)
            return AnayasaId(kind=kind, year=year, sequence=sequence)
        raise InvalidDocumentId(value, "anayasa türü 'nd' veya 'bb' olmalıdır")

    if parts and parts[0] in _OPAQUE_NAMESPACES | {"anayasa"}:
        raise InvalidDocumentId(value, "belge kimliği yanlış sayıda bölüm içeriyor")
    raise InvalidDocumentId(value, f"bilinmeyen belge kimliği ad alanı '{parts[0]}'")


def format_document_id(document_id: DocumentId) -> str:
    return document_id.format()


def _is_url(value: str) -> bool:
    lowered = value.lower()
    return (
        "://" in value
        or value.startswith(("/", "\\", "//"))
        or lowered.startswith(("http:", "https:", "file:", "ftp:"))
    )


def _require_opaque(opaque: str, raw: str | None = None) -> None:
    if not opaque or any(ch not in _OPAQUE_CHARS for ch in opaque):
        raise InvalidDocumentId(
            raw if raw is not None else opaque,
            "opak kimlik, boş olmayan bir [A-Za-z0-9._-]+ belirteci olmalıdır",
        )


def _is_anayasa_kind(kind: str) -> TypeGuard[Literal["nd", "bb"]]:
    return kind in _ANAYASA_KINDS


def _parse_anayasa_year(text: str, raw: str) -> int:
    if len(text) != 4 or not text.isdigit() or text[0] == "0":
        raise InvalidDocumentId(raw, "anayasa yılı 4 basamaklı bir tam sayı olmalıdır")
    return int(text)


def _parse_anayasa_sequence(text: str, raw: str) -> int:
    if (
        not text.isascii()
        or not text.isdigit()
        or text[0] == "0"
        or len(text) > ANAYASA_SEQUENCE_MAX_DIGITS
    ):
        raise InvalidDocumentId(
            raw,
            (
                "anayasa sıra numarası başında sıfır bulunmayan pozitif bir tam sayı "
                f"ve en fazla {_MAX_ANAYASA_SEQUENCE} olmalıdır"
            ),
        )
    sequence = int(text)
    if sequence > _MAX_ANAYASA_SEQUENCE:
        raise InvalidDocumentId(
            raw, f"anayasa sıra numarası {_MAX_ANAYASA_SEQUENCE}'den büyük olmamalıdır"
        )
    return sequence
