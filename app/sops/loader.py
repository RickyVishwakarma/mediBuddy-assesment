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

# Cached against a fingerprint of the directory rather than indefinitely. Policy files
# are the one thing here designed to be edited by someone who never touches the Python,
# so a cache that outlives an edit is a trap: a new rule appears to have been added and
# silently never fires until someone restarts the process.
#
# That happened. A running server held thirteen policies while the directory had
# fourteen, and the answer to an ordinary question was "we have no guidance". Watching
# the files from uvicorn was the obvious fix and does not work reliably -- its reloader
# is oriented at .py -- so the check belongs here, where it cannot be forgotten.
#
# The fingerprint is name + mtime + size across the directory: one stat() per file, a
# few microseconds against a request that will spend hundreds of milliseconds on a
# weather call and an LLM call.
_CACHE: dict[str, tuple[tuple, list[SOP]]] = {}


def _fingerprint(path: Path) -> tuple:
    return tuple(
        sorted(
            (f.name, f.stat().st_mtime_ns, f.stat().st_size)
            for f in list(path.glob("*.yaml")) + list(path.glob("*.yml"))
        )
    )


def load_policies(directory: Path | None = None, use_cache: bool = True) -> list[SOP]:
    """Load and validate every *.yaml in the policy directory.

    Raises on the first bad file. A policy set that half-loads is worse than one that
    refuses to start, because the missing rule is invisible at runtime.

    Re-reads automatically when a file is added, edited or removed, so a policy dropped
    into the directory takes effect on the next question with no restart.
    """
    path = Path(directory or POLICY_DIR)
    key = str(path.resolve())

    if not path.is_dir():
        raise SOPValidationError(f"policy directory not found: {path}")

    stamp = _fingerprint(path)
    if use_cache and key in _CACHE and _CACHE[key][0] == stamp:
        return _CACHE[key][1]

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

    _CACHE[key] = (stamp, policies)
    return policies


def clear_cache() -> None:
    _CACHE.clear()
