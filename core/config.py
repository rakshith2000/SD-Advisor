"""Configuration loading.

Non-secret settings live in config/conf.json. Every credential comes from Vault
at runtime, so nothing sensitive is ever committed or sitting in the repo.
"""

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / 'config' / 'conf.json'

_lock = threading.Lock()
_settings: Optional['Settings'] = None


class ConfigError(RuntimeError):
    pass


class Settings:
    """Thin dotted-path accessor over the config dict.

    Kept deliberately simple: config is read once at startup and treated as
    immutable. Anything that needs to change at runtime (weights, thresholds
    per customer) belongs in the database, not here.
    """

    def __init__(self, raw: Dict[str, Any], path: Path):
        self._raw = raw
        self.path = path
        self.root = PROJECT_ROOT

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._raw
        for part in dotted.split('.'):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

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
        return json.loads(json.dumps(self._raw))


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
