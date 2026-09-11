"""Belge içi mevzuat araması için Boolean ayrıştırma ve değerlendirme."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum

_TURKISH_CASE_TRANSLATION: dict[str, str | int | None] = {
    "I": "ı",
    "İ": "i",
}
_MAX_NESTING_DEPTH = 64


class BooleanQueryError(ValueError):
    """Belge içi Boolean ifadesi hatalı biçimlendirildiğinde oluşturulur."""


class TokenKind(StrEnum):
    TERM = "TERM"
    PHRASE = "PHRASE"
    AND = "AND"
    OR = "OR"
    NOT = "NOT"
    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    EOF = "EOF"


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    value: str
    offset: int


def turkish_fold(value: str) -> str:
    """Türkçe noktalı/noktasız I ayrımını koruyarak metni büyük/küçük harften bağımsızlaştırır."""
    return unicodedata.normalize(
        "NFKC", value.translate(str.maketrans(_TURKISH_CASE_TRANSLATION))
    ).casefold()


class Expression:
    """Madde metnine karşı değerlendirilen derlenmiş Boolean ifadesi."""

    def matches(self, text: str) -> bool:
        raise NotImplementedError

    def occurrence_count(self, text: str) -> int:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Literal(Expression):
    value: str

    def matches(self, text: str) -> bool:
        return turkish_fold(self.value) in turkish_fold(text)

    def occurrence_count(self, text: str) -> int:
        needle = turkish_fold(self.value)
        if not needle:
            return 0
        return turkish_fold(text).count(needle)


@dataclass(frozen=True, slots=True)
class Not(Expression):
    child: Expression

    def matches(self, text: str) -> bool:
        return not self.child.matches(text)

    def occurrence_count(self, text: str) -> int:
        return 0


@dataclass(frozen=True, slots=True)
class And(Expression):
    left: Expression
    right: Expression

    def matches(self, text: str) -> bool:
        return self.left.matches(text) and self.right.matches(text)

    def occurrence_count(self, text: str) -> int:
        return self.left.occurrence_count(text) + self.right.occurrence_count(text)


@dataclass(frozen=True, slots=True)
class Or(Expression):
    left: Expression
    right: Expression

    def matches(self, text: str) -> bool:
        return self.left.matches(text) or self.right.matches(text)

    def occurrence_count(self, text: str) -> int:
        return self.left.occurrence_count(text) + self.right.occurrence_count(text)


@dataclass(frozen=True, slots=True)
class BooleanQuery:
    """V1 öncelik ve örtük AND kurallarına göre ayrıştırılmış sorgu."""

    expression: Expression

    def matches(self, text: str) -> bool:
        return self.expression.matches(text)

    def match_count(self, text: str) -> int:
        """Return a positive count for an article accepted by the expression.

        A purely negative expression (for example ``NOT yürürlük``) has no
        positive literal occurrence, but a returned within-hit contract still
        requires a positive count. Its satisfied predicate therefore counts as
        one match.
        """
        if not self.matches(text):
            return 0
        return max(1, self.expression.occurrence_count(text))

    def literals(self) -> tuple[str, ...]:
        """Return leaf terms in source order for local result snippets."""
        return tuple(_literal_values(self.expression))


def _literal_values(expression: Expression) -> tuple[str, ...]:
    if isinstance(expression, Literal):
        return (expression.value,)
    if isinstance(expression, Not):
        return _literal_values(expression.child)
    if isinstance(expression, (And, Or)):
        return _literal_values(expression.left) + _literal_values(expression.right)
    return ()


def tokenize(query: str) -> tuple[Token, ...]:
    """Sorguyu tırnak ve parantez söz dizimini denetleyerek simgelere ayırır."""
    if not isinstance(query, str) or not query.strip():
        raise BooleanQueryError("Sorgu en az bir terim içermelidir.")

    tokens: list[Token] = []
    position = 0
    length = len(query)
    while position < length:
        character = query[position]
        if character.isspace():
            position += 1
            continue
        if character == "(":
            tokens.append(Token(TokenKind.LPAREN, character, position))
            position += 1
            continue
        if character == ")":
            tokens.append(Token(TokenKind.RPAREN, character, position))
            position += 1
            continue
        if character == '"':
            start = position
            position += 1
            content: list[str] = []
            while position < length:
                current = query[position]
                if current == "\\":
                    if position + 1 >= length or query[position + 1] not in {'"', "\\"}:
                        raise BooleanQueryError(
                            f"Geçersiz kaçış karakteri; konum: {position}."
                        )
                    content.append(query[position + 1])
                    position += 2
                    continue
                if current == '"':
                    position += 1
                    break
                content.append(current)
                position += 1
            else:
                raise BooleanQueryError(
                    f"Tırnak içindeki ifade sonlandırılmamış; konum: {start}."
                )
            phrase = "".join(content)
            if not phrase.strip():
                raise BooleanQueryError(
                    f"Tırnak içindeki ifade boş olamaz; konum: {start}."
                )
            tokens.append(Token(TokenKind.PHRASE, phrase, start))
            continue

        start = position
        while (
            position < length
            and not query[position].isspace()
            and query[position] not in '()"'
        ):
            position += 1
        term = query[start:position]
        if not term:
            raise BooleanQueryError(f"Beklenmeyen karakter; konum: {position}.")
        operator = {"AND": TokenKind.AND, "OR": TokenKind.OR, "NOT": TokenKind.NOT}.get(
            term
        )
        tokens.append(Token(operator or TokenKind.TERM, term, start))

    tokens.append(Token(TokenKind.EOF, "", length))
    return tuple(tokens)


class _Parser:
    def __init__(self, tokens: tuple[Token, ...]) -> None:
        self._tokens = tokens
        self._position = 0

    @property
    def current(self) -> Token:
        return self._tokens[self._position]

    def parse(self) -> Expression:
        expression = self._parse_or(0)
        if self.current.kind is not TokenKind.EOF:
            raise BooleanQueryError(
                f"Beklenmeyen {self.current.value!r} ifadesi; konum: {self.current.offset}."
            )
        return expression

    def _advance(self) -> Token:
        token = self.current
        self._position += 1
        return token

    def _parse_or(self, nesting_depth: int) -> Expression:
        expressions = [self._parse_and(nesting_depth)]
        while self.current.kind is TokenKind.OR:
            operator = self._advance()
            if not _begins_unary(self.current.kind):
                raise BooleanQueryError(
                    f"OR işleci için sağ taraf terimi gerekir; konum: {operator.offset}."
                )
            expressions.append(self._parse_and(nesting_depth))
        return _balanced_binary(expressions, Or)

    def _parse_and(self, nesting_depth: int) -> Expression:
        expressions = [self._parse_unary(nesting_depth)]
        while True:
            if self.current.kind is TokenKind.AND:
                operator = self._advance()
                if not _begins_unary(self.current.kind):
                    raise BooleanQueryError(
                        f"AND işleci için sağ taraf terimi gerekir; konum: {operator.offset}."
                    )
                expressions.append(self._parse_unary(nesting_depth))
            elif _begins_unary(self.current.kind):
                expressions.append(self._parse_unary(nesting_depth))
            else:
                return _balanced_binary(expressions, And)

    def _parse_unary(self, nesting_depth: int) -> Expression:
        if self.current.kind is TokenKind.NOT:
            if nesting_depth >= _MAX_NESTING_DEPTH:
                raise BooleanQueryError(
                    f"Sorgu en fazla {_MAX_NESTING_DEPTH} düzey iç içe ifade "
                    f"içerebilir; konum: {self.current.offset}."
                )
            operator = self._advance()
            if not _begins_unary(self.current.kind):
                raise BooleanQueryError(
                    f"NOT işleci için terim gerekir; konum: {operator.offset}."
                )
            return Not(self._parse_unary(nesting_depth + 1))
        return self._parse_primary(nesting_depth)

    def _parse_primary(self, nesting_depth: int) -> Expression:
        token = self.current
        if token.kind in {TokenKind.TERM, TokenKind.PHRASE}:
            self._advance()
            return Literal(token.value)
        if token.kind is TokenKind.LPAREN:
            if nesting_depth >= _MAX_NESTING_DEPTH:
                raise BooleanQueryError(
                    f"Sorgu en fazla {_MAX_NESTING_DEPTH} düzey iç içe ifade "
                    f"içerebilir; konum: {token.offset}."
                )
            self._advance()
            if self.current.kind is TokenKind.RPAREN:
                raise BooleanQueryError(f"Boş parantez; konum: {token.offset}.")
            expression = self._parse_or(nesting_depth + 1)
            if self.current.kind is not TokenKind.RPAREN:
                raise BooleanQueryError(f"Kapanmamış parantez; konum: {token.offset}.")
            self._advance()
            return expression
        if token.kind is TokenKind.RPAREN:
            raise BooleanQueryError(f"Beklenmeyen ')'; konum: {token.offset}.")
        if token.kind in {TokenKind.AND, TokenKind.OR}:
            raise BooleanQueryError(
                f"Beklenmeyen {token.value!r} işleci; konum: {token.offset}."
            )
        raise BooleanQueryError(f"Terim bekleniyordu; konum: {token.offset}.")


def _begins_unary(kind: TokenKind) -> bool:
    return kind in {TokenKind.TERM, TokenKind.PHRASE, TokenKind.NOT, TokenKind.LPAREN}


def _balanced_binary(
    expressions: list[Expression], constructor: type[And | Or]
) -> Expression:
    """Combine an associative chain without a left-deep AST."""
    levels: list[Expression | None] = []
    for expression in expressions:
        level = 0
        while level < len(levels):
            previous = levels[level]
            if previous is None:
                break
            expression = constructor(previous, expression)
            levels[level] = None
            level += 1
        if level == len(levels):
            levels.append(expression)
        else:
            levels[level] = expression

    combined: Expression | None = None
    for level_expression in levels:
        if level_expression is not None:
            combined = (
                level_expression
                if combined is None
                else constructor(level_expression, combined)
            )
    assert combined is not None
    return combined


def parse_boolean_query(query: str) -> BooleanQuery:
    """V1 Boolean sorgu lehçesini ayrıştırır.

    Öncelik sırası parantez, NOT, AND (bitişik terimler dâhil), ardından OR'dur.
    Büyük harfli AND/OR/NOT işleçtir; diğer sözcükler sıradan arama terimleridir.
    """
    return BooleanQuery(_Parser(tokenize(query)).parse())
