"""
Frozen strategy bundles: what the live trader actually runs.

A bundle is an immutable on-disk copy of one strategy version -- the generated source with a small live wrapper
appended, a manifest, and a hash over both. It is written once at promotion approval, marked read-only, and
verified by the trader at start-up and continually while running. The research side never writes here, and a
live strategy has no code path that can modify itself or its bundle.

The live wrapper (appended, strategy logic untouched) makes the Risk Engine gate MANDATORY and FRESH for live
use: a missing, unreadable, stale (watchdog died) or halted gate means no entries.
"""

import hashlib
import json
import os
import stat
import time

from .. import strategies as S
from . import paths

WRAPPER = '''

# --- Live deployment wrapper (appended by Jarvis; the strategy logic above is unchanged) -----------------
import time as _time


def _risk_gate_allows():
    """LIVE: the Risk Engine gate is mandatory and must be fresh. Missing, unreadable, stale or halted -> no entries."""
    path = os.environ.get("JARVIS_RISK_GATE_FILE")
    if not path:
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            gate = json.load(f)
        if _time.time() - float(gate["ts"]) > {max_age}:
            return False
        return bool(gate.get("allow_entries", False)) and not bool(gate.get("halted", True))
    except Exception:
        return False
'''


class BundleError(Exception):
    pass


def bundle_path(strategy_id, version):
    return os.path.join(paths.BUNDLE_DIR, f"{strategy_id}_v{version}")


def live_source(source, max_age_seconds):
    return source + WRAPPER.replace("{max_age}", str(int(max_age_seconds)))


def compute_hash(source_bytes, registry_content_hash, gate_max_age_seconds):
    return hashlib.sha256(source_bytes + b"|" + registry_content_hash.encode() + b"|" + str(int(gate_max_age_seconds)).encode()).hexdigest()


def freeze(registry, strategy_id, version, limits, review_id=None, base_dir=None):
    """Writes (once) the frozen bundle for a strategy version and returns its manifest."""
    meta = registry.get(strategy_id)
    stored = registry.get_version(strategy_id, version)
    if not meta or not stored:
        raise BundleError(f"Unknown strategy {strategy_id} v{version}")
    template = meta["source"].split(":", 1)[1]
    if S.is_ml(template):
        raise BundleError("ML strategies run in the lab's own simulator and cannot be deployed live.")
    class_name, regenerated, _ = S.generate(template, stored["parameters"])
    if regenerated != stored["code"]:
        raise BundleError("The stored code does not regenerate from its template and parameters; refusing to freeze it.")
    source = live_source(stored["code"], limits.gate_max_age_seconds)
    manifest = {"strategy_id": strategy_id, "version": version, "class_name": class_name, "template": template,
                "parameters": stored["parameters"], "registry_content_hash": stored["content_hash"],
                "gate_max_age_seconds": int(limits.gate_max_age_seconds), "review_id": review_id,
                "bundle_hash": compute_hash(source.encode("utf-8"), stored["content_hash"], limits.gate_max_age_seconds)}
    root = base_dir or paths.BUNDLE_DIR
    folder = os.path.join(root, f"{strategy_id}_v{version}")
    if os.path.exists(folder):
        ok, problems = verify(folder)
        if ok:
            with open(os.path.join(folder, "manifest.json"), "r", encoding="utf-8") as f:
                existing = json.load(f)
            if existing["bundle_hash"] == manifest["bundle_hash"]:
                return existing
        raise BundleError("A different bundle already exists for this version; bundles are never overwritten.")
    os.makedirs(folder)
    manifest["created"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(os.path.join(folder, "strategy.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(source)
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    for name in ("strategy.py", "manifest.json"):
        os.chmod(os.path.join(folder, name), stat.S_IREAD)
    return manifest


def verify(folder):
    """Re-hashes a bundle on disk. Returns (ok, problems)."""
    problems = []
    try:
        with open(os.path.join(folder, "manifest.json"), "r", encoding="utf-8") as f:
            manifest = json.load(f)
        with open(os.path.join(folder, "strategy.py"), "rb") as f:
            source = f.read()
    except Exception as error:
        return False, [f"bundle unreadable: {error}"]
    expected = compute_hash(source, manifest["registry_content_hash"], manifest["gate_max_age_seconds"])
    if expected != manifest["bundle_hash"]:
        problems.append("strategy file or manifest has been modified since it was frozen")
    return not problems, problems


def load_manifest(strategy_id, version, base_dir=None):
    folder = os.path.join(base_dir or paths.BUNDLE_DIR, f"{strategy_id}_v{version}")
    with open(os.path.join(folder, "manifest.json"), "r", encoding="utf-8") as f:
        return json.load(f), folder
