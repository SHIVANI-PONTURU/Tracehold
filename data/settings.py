"""Project paths and settings.

Read from .env in the project root (when python-dotenv is installed) and then the environment.
IBM's file paths and the time zone come from synthetic/overlay_config.yaml, so there is one source of truth.
"""
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:                                     # optional: .env support
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except ImportError:
    pass


def _path(value, default):
    p = value or default
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(ROOT, p))


SYNTHETIC_DIR = os.path.join(ROOT, "synthetic")
EVAL_DIR = os.path.join(ROOT, "eval")
WORK_DIR = _path(os.environ.get("TRACEHOLD_WORK_DIR"), "work")       # generated data, safe to delete and rebuild
CLEAN_DIR = os.path.join(WORK_DIR, "clean")
REPORT_DIR = os.path.join(WORK_DIR, "reports")
SNAPSHOT_DIR = os.path.join(WORK_DIR, "snapshot")
OVERLAY_CONFIG_PATH = os.path.join(SYNTHETIC_DIR, "overlay_config.yaml")
OVERLAY_OUTPUT_DIR = os.path.join(SYNTHETIC_DIR, "output")
LABELS_DIR = os.path.join(EVAL_DIR, "labels")

FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT", "6379"))
FALKORDB_PASSWORD = os.environ.get("FALKORDB_PASSWORD") or None


def overlay_config():
    with open(OVERLAY_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def raw_paths():
    """IBM's three files: transactions, patterns, accounts."""
    cfg = overlay_config()
    return {k: os.path.normpath(os.path.join(SYNTHETIC_DIR, v)) for k, v in cfg["input"].items()}


def time_offset_minutes():
    return int(overlay_config()["time"]["source_utc_offset_minutes"])


def ensure_dirs(*dirs):
    for d in dirs:
        os.makedirs(d, exist_ok=True)
