"""The `triggerRule` expression language.

A trigger rule is a single expression that decides whether a script agent runs for an
inbound message:

    (text.contains("ABC") and text.contains("XYZ")) and channel.notContains("PODUEMCJE")

Grammar:

    expression := or_expr
    or_expr    := and_expr ( "or" and_expr )*
    and_expr   := unary ( "and" unary )*
    unary      := "not" unary | "(" expression ")" | condition
    condition  := FIELD "." OPERATOR "(" STRING ")"

Expressions are tokenised and parsed into a small AST. They are never `eval`'d — an
AGENT.md file is configuration, and evaluating it would make any agent folder a remote
code execution vector.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from .errors import StarkError

# Fields a rule may match on. These are the fields listeners populate on Message.
VALID_FIELDS = ("text", "user", "channel", "thread")

CONTAINS = "contains"
NOT_CONTAINS = "notcontains"

# Canonical spellings, used in error messages.
OPERATOR_NAMES = {CONTAINS: "contains", NOT_CONTAINS: "notContains"}

KEYWORDS = ("and", "or", "not")


class TriggerRuleError(StarkError):
    """A triggerRule expression is malformed."""


# --------------------------------------------------------------------------------------
# Tokeniser
# --------------------------------------------------------------------------------------

_TOKEN = re.compile(
    r"""
      (?P<space>\s+)
    | (?P<lparen>\()
    | (?P<rparen>\))
    | (?P<dot>\.)
    | (?P<string>"(?:[^"\\]|\\.)*")
    | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)

_UNTERMINATED = re.compile(r'"(?:[^"\\]|\\.)*$')


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    position: int


def _unescape(literal: str) -> str:
    """Turn a quoted token into its string value, honouring \\" and \\\\."""
    return re.sub(r"\\(.)", r"\1", literal[1:-1])


def tokenize(expression: str) -> list[Token]:
    tokens: list[Token] = []
    index = 0

    while index < len(expression):
        match = _TOKEN.match(expression, index)
        if match is None:
            if _UNTERMINATED.match(expression, index):
                raise TriggerRuleError(
                    f"unterminated string starting at position {index + 1} — "
                    "every literal needs a closing double quote"
                )
            raise TriggerRuleError(
                f"unexpected character {expression[index]!r} at position {index + 1}"
            )

        kind = match.lastgroup or ""
        text = match.group()
        index = match.end()

        if kind == "space":
            continue
        if kind == "string":
            tokens.append(Token("string", _unescape(text), match.start()))
        elif kind == "name" and text.lower() in KEYWORDS:
            tokens.append(Token(text.lower(), text, match.start()))
        else:
            tokens.append(Token(kind if kind != "name" else "name", text, match.start()))

    tokens.append(Token("end", "", len(expression)))
    return tokens


# --------------------------------------------------------------------------------------
# AST
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Condition:
    """One `field.operator("literal")` test."""

    field: str
    operator: str
    literal: str

    def evaluate(self, values: Mapping[str, str | None]) -> bool:
        value = values.get(self.field)
        # An absent field cannot contain anything, so `contains` is False and
        # `notContains` is vacuously True.
        found = value is not None and self.literal.lower() in value.lower()
        return found if self.operator == CONTAINS else not found

    def fields(self) -> set[str]:
        return {self.field}

    def __str__(self) -> str:
        return f'{self.field}.{OPERATOR_NAMES[self.operator]}("{self.literal}")'


@dataclass(frozen=True)
class Not:
    operand: "Node"

    def evaluate(self, values: Mapping[str, str | None]) -> bool:
        return not self.operand.evaluate(values)

    def fields(self) -> set[str]:
        return self.operand.fields()

    def __str__(self) -> str:
        return f"not {self.operand}"


@dataclass(frozen=True)
class And:
    operands: tuple["Node", ...]

    def evaluate(self, values: Mapping[str, str | None]) -> bool:
        return all(operand.evaluate(values) for operand in self.operands)

    def fields(self) -> set[str]:
        return {name for operand in self.operands for name in operand.fields()}

    def __str__(self) -> str:
        return "(" + " and ".join(str(operand) for operand in self.operands) + ")"


@dataclass(frozen=True)
class Or:
    operands: tuple["Node", ...]

    def evaluate(self, values: Mapping[str, str | None]) -> bool:
        return any(operand.evaluate(values) for operand in self.operands)

    def fields(self) -> set[str]:
        return {name for operand in self.operands for name in operand.fields()}

    def __str__(self) -> str:
        return "(" + " or ".join(str(operand) for operand in self.operands) + ")"


Node = Condition | Not | And | Or


# --------------------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------------------


class _Parser:
    def __init__(self, tokens: Sequence[Token]):
        self.tokens = tokens
        self.index = 0

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def advance(self) -> Token:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def expect(self, kind: str, description: str) -> Token:
        if self.current.kind != kind:
            raise TriggerRuleError(
                f"expected {description} at position {self.current.position + 1}, "
                f"found {self._describe(self.current)}"
            )
        return self.advance()

    @staticmethod
    def _describe(token: Token) -> str:
        return "end of expression" if token.kind == "end" else repr(token.value)

    def parse(self) -> Node:
        node = self.parse_or()
        if self.current.kind != "end":
            raise TriggerRuleError(
                f"unexpected {self._describe(self.current)} at position "
                f"{self.current.position + 1} — check for a missing 'and' or 'or'"
            )
        return node

    def parse_or(self) -> Node:
        operands = [self.parse_and()]
        while self.current.kind == "or":
            self.advance()
            operands.append(self.parse_and())
        return operands[0] if len(operands) == 1 else Or(tuple(operands))

    def parse_and(self) -> Node:
        operands = [self.parse_unary()]
        while self.current.kind == "and":
            self.advance()
            operands.append(self.parse_unary())
        return operands[0] if len(operands) == 1 else And(tuple(operands))

    def parse_unary(self) -> Node:
        if self.current.kind == "not":
            self.advance()
            return Not(self.parse_unary())

        if self.current.kind == "lparen":
            self.advance()
            node = self.parse_or()
            self.expect("rparen", "')'")
            return node

        return self.parse_condition()

    def parse_condition(self) -> Node:
        name = self.expect("name", "a field name such as 'text'")
        field = name.value.lower()
        if field not in VALID_FIELDS:
            raise TriggerRuleError(
                f"unknown field {name.value!r} at position {name.position + 1} — "
                f"expected one of {', '.join(VALID_FIELDS)}"
            )

        self.expect("dot", f"'.' after '{name.value}'")

        operator_token = self.expect("name", "an operator such as 'contains'")
        operator = operator_token.value.lower()
        if operator not in OPERATOR_NAMES:
            raise TriggerRuleError(
                f"unknown operator {operator_token.value!r} at position "
                f"{operator_token.position + 1} — expected "
                f"{' or '.join(OPERATOR_NAMES.values())}"
            )

        self.expect("lparen", f"'(' after '{operator_token.value}'")
        literal = self.expect("string", 'a quoted literal such as "ABC"')
        self.expect("rparen", "')'")

        if not literal.value:
            raise TriggerRuleError(
                f"empty literal at position {literal.position + 1} — "
                "an empty string matches everything, which is never intended"
            )

        return Condition(field=field, operator=operator, literal=literal.value)


@dataclass(frozen=True)
class TriggerRule:
    """A parsed triggerRule, ready to evaluate against a message."""

    source: str
    root: Node

    def matches(self, values: Mapping[str, str | None]) -> bool:
        return self.root.evaluate(values)

    def fields(self) -> set[str]:
        """The message fields this rule reads."""
        return self.root.fields()

    def __str__(self) -> str:
        return self.source


def parse(expression: str) -> TriggerRule:
    """Parse a triggerRule expression.

    Raises `TriggerRuleError` with a position when the expression is malformed. Callers
    parse at load time so a bad rule is reported at startup rather than on the first
    message that would have matched it.
    """
    if not isinstance(expression, str):
        raise TriggerRuleError(
            f"triggerRule must be a string, got {type(expression).__name__} — "
            "wrap the expression in quotes"
        )

    text = expression.strip()
    if not text:
        raise TriggerRuleError("triggerRule is empty")

    return TriggerRule(source=text, root=_Parser(tokenize(text)).parse())
