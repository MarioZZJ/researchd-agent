"""Deterministic command parsing (IMPLEMENTATION.md §18).

First version: deterministic commands take priority. Anything that is not a
known deterministic command is returned as `UnknownCommand`; the ACP shim may
then (if enabled) route it to an interaction profile for intent classification —
never to direct database access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParsedCommand:
    name: str
    args: list[str] = field(default_factory=list)
    flags: dict[str, Any] = field(default_factory=dict)
    raw: str = ""


class UnknownCommand(Exception):
    def __init__(self, text: str):
        super().__init__(f"unknown command: {text!r}")
        self.text = text


FLAG_RE = re.compile(r"^(--[a-z-]+)(?:=(.*))?$", re.IGNORECASE)

# name -> (arity range, allowed flags)
COMMAND_SPECS: dict[str, tuple[tuple[int, int], set[str]]] = {
    "bind": ((1, 2), set()),  # project <id> | inbox
    "status": ((0, 0), set()),
    "pause": ((0, 0), set()),
    "resume": ((0, 0), set()),
    "digest": ((0, 0), set()),
    "sync": ((0, 0), set()),
    "model": ((0, 2), set()),  # [interaction fast|deep|deterministic]
    "config": ((1, 3), set()),  # show | set role.<role> <profile>
    "decision": ((2, 2), {"--version"}),
    "explain": ((1, 1), set()),
    "task": ((1, 1), set()),
    "claim": ((1, 1), set()),
}

PREFIXES = ("/research", "/decision", "/explain", "/task", "/claim")


def parse_command(text: str) -> ParsedCommand:
    """Parse a command line like `/research status` or
    `/decision D-002 B --version 3`. Raises UnknownCommand when the first token
    is not a known command root."""
    stripped = text.strip()
    if not stripped:
        raise UnknownCommand(text)
    tokens = stripped.split()
    root = tokens[0].lower()

    if root in ("/research", "/researchd"):
        if len(tokens) < 2:
            raise UnknownCommand(text)
        name = tokens[1].lower()
    elif root in ("/decision", "/explain", "/task", "/claim"):
        name = root[1:]
    else:
        raise UnknownCommand(text)

    spec = COMMAND_SPECS.get(name)
    if spec is None:
        raise UnknownCommand(text)

    args: list[str] = []
    flags: dict[str, Any] = {}
    arg_start = 2 if root in ("/research", "/researchd") else 1
    rest = tokens[arg_start:]
    i = 0
    while i < len(rest):
        tok = rest[i]
        m = FLAG_RE.match(tok)
        if m:
            flag = m.group(1)
            if flag not in spec[1]:
                raise UnknownCommand(f"{text} (unknown flag {flag})")
            value = m.group(2)
            if value is None:
                # support "--flag value" (space-separated)
                if i + 1 < len(rest) and not FLAG_RE.match(rest[i + 1]):
                    value = rest[i + 1]
                    i += 1
                else:
                    value = True
            flags[flag] = value
        else:
            args.append(tok)
        i += 1

    (lo, hi) = spec[0]
    if not (lo <= len(args) <= hi):
        raise UnknownCommand(f"{text} (expected {lo}-{hi} args, got {len(args)})")
    return ParsedCommand(name=name, args=args, flags=flags, raw=stripped)
