from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class PolicyLoader:
    def __init__(self, engine_root: Path) -> None:
        self.engine_root = Path(engine_root)
        self.domains_root = self.engine_root / "domains"

    def load_yaml(self, path: Path) -> dict[str, Any]:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def load_common(self) -> dict[str, Any]:
        return self.load_yaml(self.domains_root / "common" / "common.yaml")

    def load_domain(self, domain_id: str) -> dict[str, Any]:
        domain_path = self.domains_root / domain_id / "domain.yaml"
        return self.load_yaml(domain_path)
