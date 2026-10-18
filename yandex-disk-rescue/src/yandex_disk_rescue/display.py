"""Text safe to show on a terminal or to write to the log file: a remote name can hold any character, and an escape
sequence, a carriage return or a newline in one must never reach the terminal or the log as itself. Every place that
writes text for a person to read escapes it here, whatever the text quotes: a path, or a reason naming a local file.

A control character (C0, DEL, C1, and the line and paragraph separators U+2028 and U+2029) is shown as its escape,
`\\x1b` or `\\u2028`, and a lone surrogate, which no output encoding takes, as `\\udcxx`.
"""

import re

_CONTROLS = re.compile('[\x00-\x1f\x7f-\x9f  ]')
_CONTROLS_BUT_NEWLINE = re.compile('[\x00-\x09\x0b-\x1f\x7f-\x9f  ]')


def display_line(text: str) -> str:
    """`text` safe to show as one line: every control character, the newline included, and lone surrogate escaped."""
    return _escaped(_CONTROLS, text)


def display_text(text: str) -> str:
    """`text` safe to show as lines: every control character but the newline, and every lone surrogate, escaped."""
    return _escaped(_CONTROLS_BUT_NEWLINE, text)


def _escaped(controls: re.Pattern[str], text: str) -> str:
    shown = controls.sub(_escape, text)
    return shown.encode('utf-8', 'backslashreplace').decode('utf-8')


def _escape(match: re.Match[str]) -> str:
    code = ord(match.group())
    return f'\\x{code:02x}' if code < 0x100 else f'\\u{code:04x}'
