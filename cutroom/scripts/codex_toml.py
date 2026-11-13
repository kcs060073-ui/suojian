"""Conservative 剪辑工作台-only TOML edits with semantic preservation checks.

The stdlib parser validates both documents. A small lexer identifies table
boundaries without interpreting quoted keys or strings as configuration. Inline
or dotted assignments defining the target are rejected rather than rewritten.
"""
from __future__ import annotations

import base64
import copy
import json
import math
import sys
import tomllib


def _header_path(header: str) -> tuple[str, ...]:
    document = tomllib.loads(header)
    parts = []
    while document:
        if not isinstance(document, dict) or len(document) != 1:
            raise ValueError('Unsupported TOML table header')
        key, document = next(iter(document.items()))
        parts.append(key)
        if isinstance(document, list):
            document = document[0]
    return tuple(parts)


def _table_lines(text: str):
    quote = None
    depth = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        if quote is None and depth == 0 and line.lstrip().startswith('['):
            yield offset, _header_path(line)
            offset += len(line)
            continue
        index = 0
        while index < len(line):
            char = line[index]
            if quote:
                if quote[0] == '"' and char == '\\':
                    index += 2
                    continue
                if line.startswith(quote, index):
                    width = len(quote)
                    if width == 3:
                        # TOML permits a closing triple plus one/two literal quotes.
                        while index + width < len(line) and line[index + width] == quote[0]:
                            width += 1
                    quote = None
                    index += width
                    continue
            elif char == '#':
                break
            elif char in ('"', "'"):
                quote = char * 3 if line.startswith(char * 3, index) else char
                index += len(quote)
                continue
            elif char in '[{':
                depth += 1
            elif char in ']}':
                depth -= 1
            index += 1
        offset += len(line)


def _without_target(document: dict) -> dict:
    result = copy.deepcopy(document)
    servers = result.get('mcp_servers')
    if servers is not None:
        if not isinstance(servers, dict):
            raise ValueError('mcp_servers must be a table')
        servers.pop('cutroom', None)
        if not servers:
            result.pop('mcp_servers')
    return result


def _comparable(value):
    if isinstance(value, float) and math.isnan(value):
        return ('TOML nan',)
    if isinstance(value, dict):
        return {key: _comparable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_comparable(item) for item in value]
    return value


def edit_cutroom(text: str, block: str = '') -> str:
    original = tomllib.loads(text)
    expected = _without_target(original)
    boundaries = list(_table_lines(text))
    spans = [(start, boundaries[index + 1][0] if index + 1 < len(boundaries) else len(text))
             for index, (start, path) in enumerate(boundaries)
             if path[:2] == ('mcp_servers', 'cutroom')]
    proposed = text
    for start, end in reversed(spans):
        proposed = proposed[:start] + proposed[end:]
    removed = tomllib.loads(proposed)
    # Fail closed for unsupported inline/dotted target definitions or a lexical
    # ambiguity. Never write a best-effort partial deletion.
    if _comparable(removed) != _comparable(expected):
        # Removing the last child can leave an explicit empty mcp_servers table.
        if ('cutroom' in removed.get('mcp_servers', {}) or
                _comparable(_without_target(removed)) != _comparable(expected)):
            raise ValueError('Cannot safely edit this TOML layout; configuration was not changed')
    if block:
        replacement = tomllib.loads(block)
        if set(replacement) != {'mcp_servers'} or set(replacement['mcp_servers']) != {'cutroom'}:
            raise ValueError('Replacement must contain only mcp_servers.cutroom')
        proposed = proposed.rstrip() + ('\n\n' if proposed.strip() else '') + block.rstrip() + '\n'
        parsed = tomllib.loads(proposed)
        if (_comparable(_without_target(parsed)) != _comparable(expected)
                or parsed['mcp_servers']['cutroom'] != replacement['mcp_servers']['cutroom']):
            raise ValueError('Non-target TOML configuration changed')
    return proposed


def main() -> int:
    try:
        # Windows PowerShell 5.1 may prepend its UTF-8 BOM to native stdin.
        encoded = sys.stdin.buffer.read().decode('utf-8-sig').strip()
        payload = json.loads(base64.b64decode(encoded, validate=True))
        output = edit_cutroom(payload['text'], payload.get('block') or '')
        print(base64.b64encode(output.encode('utf-8')).decode('ascii'))
        return 0
    except (ValueError, TypeError, KeyError):
        # Parser diagnostics can contain configuration fragments. Do not echo them.
        print('Cannot safely parse/edit Codex TOML; no configuration was changed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
