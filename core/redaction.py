"""PII / secret redaction applied before any ticket text reaches the LLM.

The existing audit tool sends raw ticket bodies - including caller phone
numbers, employee IDs and occasionally credentials - straight to Azure OpenAI.
Here we substitute stable placeholders on the way out and restore them on the
way back, so drafted work notes still read naturally to the agent while the
model never sees the real values.

Restoration is deliberately scoped to a single ticket: each Redactor instance
holds its own mapping and is discarded afterwards.
"""

import re
from typing import Dict, List, Tuple

# Order matters - the more specific patterns run first so a credit card is not
# first eaten by the generic long-digit-run rule.
_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ('EMAIL', re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b')),
    ('CARD', re.compile(r'\b(?:\d[ -]?){13,19}\b')),
    ('SSN', re.compile(r'\b\d{3}-\d{2}-\d{4}\b')),
    ('IBAN', re.compile(r'\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b')),
    ('APIKEY', re.compile(
        r'\b(?:sk-|pk-|ghp_|xox[baprs]-|AKIA|ASIA)[A-Za-z0-9_\-]{12,}\b')),
    ('JWT', re.compile(r'\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b')),
    ('PRIVATEKEY', re.compile(
        r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----',
        re.DOTALL)),
    ('PHONE', re.compile(
        r'(?<![\w.])(?:\+?\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?)?\d{3,4}[ .-]?\d{3,4}(?:[ .-]?\d{2,4})?(?![\w.])')),
    ('IPADDR', re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')),
]

# Credential assignments: "password: Hunter2", "pwd = abc123", "passcode is X"
_SECRET_ASSIGNMENT = re.compile(
    r'\b(password|passwd|pwd|passcode|secret|token|api[ _-]?key|credential)s?\b'
    r'\s*(?:is|are|:|=|->)\s*([^\s,;"\'\n]{4,})',
    re.IGNORECASE,
)


class Redactor:
    """Two-way redaction for one ticket's worth of text."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._forward: Dict[str, str] = {}   # real value -> placeholder
        self._reverse: Dict[str, str] = {}   # placeholder -> real value
        self._counters: Dict[str, int] = {}

    # -- public -----------------------------------------------------------

    def scrub(self, text: object) -> str:
        """Replace sensitive values with stable placeholders."""
        if text is None:
            return ''
        value = str(text)
        if not self.enabled or not value.strip():
            return value

        value = _SECRET_ASSIGNMENT.sub(self._sub_assignment, value)
        for label, pattern in _PATTERNS:
            value = pattern.sub(lambda m, _l=label: self._placeholder(_l, m.group(0)), value)
        return value

    def scrub_mapping(self, data: Dict[str, object]) -> Dict[str, object]:
        """Scrub every string value in a flat-ish dict, recursing into lists."""
        cleaned: Dict[str, object] = {}
        for key, value in data.items():
            if isinstance(value, str):
                cleaned[key] = self.scrub(value)
            elif isinstance(value, dict):
                cleaned[key] = self.scrub_mapping(value)
            elif isinstance(value, list):
                cleaned[key] = [
                    self.scrub_mapping(v) if isinstance(v, dict)
                    else self.scrub(v) if isinstance(v, str)
                    else v
                    for v in value
                ]
            else:
                cleaned[key] = value
        return cleaned

    def restore(self, text: object) -> str:
        """Put the real values back into LLM-generated output."""
        if text is None:
            return ''
        value = str(text)
        if not self.enabled or not self._reverse:
            return value
        # Longest placeholders first so [[PHONE_10]] is not clipped by [[PHONE_1]].
        for placeholder in sorted(self._reverse, key=len, reverse=True):
            value = value.replace(placeholder, self._reverse[placeholder])
        return value

    @property
    def redaction_count(self) -> int:
        return len(self._reverse)

    # -- internals ---------------------------------------------------------

    def _sub_assignment(self, match: re.Match) -> str:
        keyword, secret = match.group(1), match.group(2)
        return f'{keyword}: {self._placeholder("SECRET", secret)}'

    def _placeholder(self, label: str, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        if value in self._forward:
            return self._forward[value]

        index = self._counters.get(label, 0) + 1
        self._counters[label] = index
        token = f'[[{label}_{index}]]'

        self._forward[value] = token
        self._reverse[token] = value
        return token


def quick_scrub(text: object) -> str:
    """One-shot scrub where restoration is not needed (e.g. log lines)."""
    return Redactor(enabled=True).scrub(text)
