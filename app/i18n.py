"""Translation helper: all German UI strings live in locales/de.yaml.

``t("task.mark_done")`` looks up a dotted key. Placeholders use ``str.format``
syntax. If a key maps to ``{one: ..., other: ...}``, the ``count`` argument
selects the plural form.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCALE_FILE = ROOT / "locales" / "de.yaml"


class Translator:
    def __init__(self, strings: dict[str, Any]) -> None:
        self._strings = strings

    @classmethod
    def from_file(cls, path: Path = DEFAULT_LOCALE_FILE) -> Translator:
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
        return cls(data)

    def lookup(self, key: str) -> Any:
        node: Any = self._strings
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                raise KeyError(f"Missing translation key: {key}")
            node = node[part]
        return node

    def __call__(self, key: str, **kwargs: Any) -> str:
        value = self.lookup(key)
        if isinstance(value, dict):
            if "count" not in kwargs:
                raise KeyError(f"Translation key {key} needs a 'count' argument")
            value = value["one"] if kwargs["count"] == 1 else value["other"]
        if not isinstance(value, str):
            raise KeyError(f"Translation key {key} is not a string")
        return value.format(**kwargs) if kwargs else value


_default: Translator | None = None


def get_translator() -> Translator:
    global _default
    if _default is None:
        _default = Translator.from_file()
    return _default


def t(key: str, **kwargs: Any) -> str:
    return get_translator()(key, **kwargs)
