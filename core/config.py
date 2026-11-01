"""Configuration loading.

Non-secret settings live in config/conf.json. Every credential comes from Vault
at runtime, so nothing sensitive is ever committed or sitting in the repo.
"""

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logging_setup import get_logger

log = get_logger('core.config')

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / 'config' / 'conf.json'

_lock = threading.Lock()
_settings: Optional['Settings'] = None

# Threshold keys renamed when the vocabulary was formalised. A configuration
# file written against the old names still works, because silently falling back
# to a default would change scoring behaviour with nothing in the logs to
# explain it.
LEGACY_THRESHOLD_KEYS = {
    'stagnation_days': 'inactivity_days',
    'critical_stagnation_days': 'prolonged_inactivity_days',
    'sla_jeopardy_pct': 'sla_risk_pct',
    'auto_close_followups': 'closure_followup_count',
    'auto_close_silence_days': 'closure_silence_days',
}


class ConfigError(RuntimeError):
    pass


def normalise_thresholds(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Accept the superseded threshold key names, warning once for each.

    The current name always wins if both are present.
    """
    thresholds = dict(raw or {})
    for legacy, current in LEGACY_THRESHOLD_KEYS.items():
        if legacy not in thresholds:
            continue
        value = thresholds.pop(legacy)
        if current in thresholds:
            log.warning(
                "Configuration contains both 'thresholds.%s' (superseded) and "
                "'thresholds.%s'; using the latter. Remove the superseded key.",
                legacy, current)
        else:
            thresholds[current] = value
            log.warning(
                "Configuration key 'thresholds.%s' has been renamed to "
                "'thresholds.%s'. The supplied value (%s) has been applied, but "
                "please update config/conf.json.", legacy, current, value)
    return thresholds


class Settings:
    """Thin dotted-path accessor over the config dict.

    Two layers, in order:

      1. A database overlay, if one has been attached - the operational keys
         an administrator can change from the settings page. See
         core.settings_store for why those live in a table rather than being
         written back into this file.
      2. config/conf.json, which is the bootstrap: the Vault address, the
         database host, the OIDC registration. Everything that has to be
         correct before the service can report that anything is wrong.

    The overlay is attached after the database exists, which is necessarily
    after the settings that describe the database have been read from the
    file. Those keys are not overridable, and that ordering is the reason.
    """

    def __init__(self, raw: Dict[str, Any], path: Path):
        self._raw = raw
        self.path = path
        self.root = PROJECT_ROOT
        self._overlay = None

    def attach_overlay(self, overlay) -> None:
        self._overlay = overlay

    @property
    def overlay(self):
        return self._overlay

    def file_value(self, dotted: str, default: Any = None) -> Any:
        """The conf.json value, ignoring any override.

        What the settings page shows beside an override, so the two cannot
        silently disagree without somebody being shown both.
        """
        node: Any = self._raw
        for part in dotted.split('.'):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def get(self, dotted: str, default: Any = None) -> Any:
        overlay = self._overlay
        if overlay is not None:
            try:
                if overlay.has(dotted):
                    return overlay.get(dotted)
            except Exception:
                # An overlay that cannot be read must not take the whole
                # service down with it; the file is a correct answer.
                log.debug('Overlay lookup failed for %s', dotted, exc_info=True)
        return self.file_value(dotted, default)

    def require(self, dotted: str) -> Any:
        value = self.get(dotted, None)
        if value in (None, ''):
            raise ConfigError(f"Missing required config key '{dotted}' in {self.path}")
        return value

    def resolve_path(self, dotted: str, default: Optional[str] = None) -> Path:
        """Resolve a config value that is a path, relative to the project root."""
        value = self.get(dotted, default)
        if value in (None, ''):
            raise ConfigError(f"Missing path config key '{dotted}'")
        candidate = Path(value)
        return candidate if candidate.is_absolute() else (self.root / candidate)

    # -- Frequently used, typed accessors ----------------------------------

    @property
    def customer_name(self) -> str:
        return self.require('customer_name')

    @property
    def shadow_mode(self) -> bool:
        return bool(self.get('runtime.shadow_mode', True))

    @property
    def assignment_groups(self) -> List[str]:
        return [g for g in self.get('servicenow.assignment_groups', []) if str(g).strip()]

    @property
    def system_accounts(self) -> List[str]:
        return [str(a).strip().lower() for a in self.get('servicenow.system_accounts', []) if str(a).strip()]

    @property
    def aged_after_days(self) -> int:
        return int(self.get('thresholds.aged_after_days', 5))

    def as_dict(self) -> Dict[str, Any]:
        """The file contents, verbatim. Overrides are not folded in."""
        return json.loads(json.dumps(self._raw))

    def effective_dict(self) -> Dict[str, Any]:
        """The file with every active override applied.

        What the service is actually running on, and what the settings page
        offers for download - so an operator who keeps conf.json under source
        control can commit the current state rather than reconstructing it.
        """
        merged = self.as_dict()
        overlay = self._overlay
        if overlay is None:
            return merged

        for dotted, value in (overlay.current() or {}).items():
            node = merged
            parts = dotted.split('.')
            for part in parts[:-1]:
                nxt = node.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[part] = nxt
                node = nxt
            node[parts[-1]] = value
        return merged


def load_settings(path: Optional[str] = None, force: bool = False) -> Settings:
    """Load (and memoise) settings.

    Path resolution order: explicit argument, then $ATA_CONFIG, then the default
    config/conf.json.
    """
    global _settings

    with _lock:
        if _settings is not None and not force and path is None:
            return _settings

        chosen = Path(path or os.environ.get('ATA_CONFIG') or DEFAULT_CONFIG_PATH)
        if not chosen.is_absolute():
            chosen = PROJECT_ROOT / chosen

        if not chosen.exists():
            raise ConfigError(
                f"Config not found at {chosen}. Copy config/conf.sample.json to "
                f"config/conf.json and edit it."
            )

        with open(chosen, 'r', encoding='utf-8') as handle:
            raw = json.load(handle)

        _settings = Settings(raw, chosen)
        return _settings


def get_settings() -> Settings:
    return _settings if _settings is not None else load_settings()
