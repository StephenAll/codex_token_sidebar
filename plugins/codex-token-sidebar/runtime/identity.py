"""Stable source identity shared by the platform launchers and runtime."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def build_identity(runtime_path: Path) -> dict:
    root = runtime_path.resolve().parent.parent
    manifest = root / ".codex-plugin/plugin.json"
    value = json.loads(manifest.read_text(encoding="utf-8"))
    version = value.get("version")
    if not isinstance(version, str) or not version or len(version) > 128:
        raise ValueError("Invalid plugin version")
    paths = [manifest, *sorted((root / "runtime").glob("*.py")),
             root / "runtime/injector.js", root / "runtime/credits_rates.json"]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return {"version": version, "fingerprint": digest.hexdigest(),
            "installationPath": str(root)}
