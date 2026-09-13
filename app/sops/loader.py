"""Discovers SOP policy files by glob.

Glob discovery is the mechanism behind the "add a policy without touching code"
requirement: a new rule is a new file in app/sops/policies/, nothing else.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from app.config import POLICY_DIR
from app.sops.schema import SOP, SOPValidationError

_CACHE: dict[str, list[SOP]] = {}


def load_policies(directory: Path | None = None, use_cache: bool = True) -> list[SOP]:
    """Load and validate every *.yaml in the policy directory.

    Raises on the first bad file. A policy set that half-loads is worse than one that
    refuses to start, because the missing rule is invisible at runtime.
    """
    path = Path(directory or POLICY_DIR)
    key = str(path.resolve())
    if use_cache and key in _CACHE:
        return _CACHE[key]

    if not path.is_dir():
        raise SOPValidationError(f"policy directory not found: {path}")

    policies: list[SOP] = []
    seen_ids: dict[str, str] = {}

    for file in sorted(path.glob("*.yaml")) + sorted(path.glob("*.yml")):
        try:
            raw = yaml.safe_load(file.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise SOPValidationError(f"{file.name}: not valid YAML -- {exc}") from exc

        if not isinstance(raw, dict):
            raise SOPValidationError(f"{file.name}: expected a single mapping at the top level")

        try:
            sop = SOP(**raw, source_file=file.name)
        except ValidationError as exc:
            raise SOPValidationError(f"{file.name}: {exc}") from exc

        if sop.id in seen_ids:
            raise SOPValidationError(
                f"{file.name}: duplicate SOP id {sop.id!r} (already defined in {seen_ids[sop.id]})"
            )
        seen_ids[sop.id] = file.name
        policies.append(sop)

    if not policies:
        raise SOPValidationError(f"no policy files found in {path}")

    _CACHE[key] = policies
    return policies


def clear_cache() -> None:
    _CACHE.clear()
