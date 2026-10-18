"""Agent settings: agents/config.yaml, with a few overrides from the environment (.env or the sandbox).

The chosen model (model.id, or TRACEHOLD_MODEL_ID) is resolved here: its provider, base URL (or the override
named by base_url_env), key variable, thinking and effort settings and prices are copied into cfg["model"], so
the rest of the code reads one place.
"""
import os

import yaml

from data import settings

PATH = os.path.join(settings.ROOT, "agents", "config.yaml")
MODEL_KEYS = ("provider", "api_key_env", "base_url_env", "reasoning_effort", "thinking", "effort",
              "price_usd_per_million", "region", "workspace_env")
CLAUDE_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "anthropic_aws": "ANTHROPIC_AWS_API_KEY",
                  "bedrock_anthropic": "BEDROCK_API_KEY"}


def resolve_model(model_cfg, model_id=None):
    """cfg["model"] with one model's settings filled in."""
    m = {k: v for k, v in model_cfg.items() if k != "models"}
    models = model_cfg.get("models", {})
    mid = model_id or os.environ.get("TRACEHOLD_MODEL_ID") or m["id"]
    spec = models.get(mid)
    if spec is None:
        raise ValueError(f"model {mid!r} is not in agents/config.yaml (model.models); known: {', '.join(models)}")
    m["id"] = mid
    m["model_id"] = spec.get("model") or mid             # the ID the API expects, when the table name differs
    m["models"] = models
    for k in MODEL_KEYS:
        m[k] = spec.get(k)
    m["provider"] = m["provider"] or "openai_compatible"
    m["api_key_env"] = m["api_key_env"] or CLAUDE_KEY_ENV.get(m["provider"], "BEDROCK_API_KEY")
    if m["provider"] == "anthropic_aws":
        m["region"] = os.environ.get("ANTHROPIC_AWS_REGION") or m["region"] or "us-east-2"
        m["workspace_env"] = m["workspace_env"] or "ANTHROPIC_AWS_WORKSPACE_ID"
    override = os.environ.get(m["base_url_env"]) if m.get("base_url_env") else None
    default = spec.get("base_url") or (f"https://aws-external-anthropic.{m['region']}.api.aws"
                                       if m["provider"] == "anthropic_aws" else None)
    m["base_url"] = override or default
    return m


def load(path=PATH):
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["tool_server_url"] = os.environ.get("TRACEHOLD_TOOL_SERVER_URL", cfg["tool_server_url"]).rstrip("/")
    cfg["bank_id"] = str(os.environ.get("TRACEHOLD_BANK_ID", cfg["bank_id"]))
    ids = os.environ.get("TRACEHOLD_BANK_IDS")
    ids = [x.strip() for x in ids.split(",")] if ids else [str(b) for b in cfg.get("bank_ids") or [cfg["bank_id"]]]
    cfg["bank_ids"] = list(dict.fromkeys([b for b in ids if b] or [cfg["bank_id"]]))
    cfg["model"] = resolve_model(cfg["model"])
    v = cfg["verifier"]
    db = os.environ.get("VERIFIER_CHECKPOINT_DB", v["checkpoint_db"])
    v["checkpoint_db"] = db if os.path.isabs(db) else os.path.join(settings.WORK_DIR, os.path.basename(db))
    return cfg
