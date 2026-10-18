"""Claude through the Anthropic Messages API, over plain HTTPS: on the Anthropic API, on Claude Platform on AWS,
or on Amazon Bedrock (the same request body; see Claude below).

The agents need three things from the model: an answer in a fixed JSON shape (structured outputs,
`output_config.format`), a loop of optional lookups (tool use with tool_choice "auto"), and the token usage for
pricing. Calling the endpoint directly keeps exactly those, adds no package, and is tested against a local stand-in
server (tests/test_anthropic_client.py).

Forced tool choice ("any" or a named tool) is never sent: Claude Opus 5.5, Sonnet 5.5 and Fable 5.1 reject it
with a 400 error. Structured outputs replace it: the API constrains the final answer to the schema.
"""
from __future__ import annotations

import copy
import json
import os
import random
import time
from urllib.parse import quote

import httpx2

VERSION = "2023-06-01"
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
UNSUPPORTED = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength",
               "maxItems", "uniqueItems", "minProperties", "maxProperties", "title")
NOTE = {"maxLength": "at most {} characters", "minLength": "at least {} characters", "maximum": "at most {}",
        "minimum": "at least {}", "maxItems": "at most {} items", "minItems": "at least {} items",
        "exclusiveMaximum": "below {}", "exclusiveMinimum": "above {}", "multipleOf": "a multiple of {}"}


class APIError(RuntimeError):
    def __init__(self, status, kind, message):
        super().__init__(f"{status} {kind}: {message}")
        self.status_code, self.kind, self.message = status, kind, message


def strict_schema(schema):
    """A pydantic JSON schema made acceptable to structured outputs: $refs inlined, additionalProperties false on
    every object, and constraints the API does not support moved into the description."""
    defs = schema.get("$defs", {}) or schema.get("definitions", {})

    def walk(node):
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            name = node["$ref"].split("/")[-1]
            return walk({**copy.deepcopy(defs[name]), **{k: v for k, v in node.items() if k != "$ref"}})
        out, notes = {}, []
        for k, v in node.items():
            if k in ("$defs", "definitions"):
                continue
            if k in UNSUPPORTED or (k == "minItems" and isinstance(v, int) and v > 1):
                if k in NOTE:
                    notes.append(NOTE[k].format(v))
                continue
            if k == "properties":
                out[k] = {pk: walk(pv) for pk, pv in v.items()}
            elif k in ("enum", "required", "const", "default"):
                out[k] = v
            else:
                out[k] = walk(v)
        if out.get("type") == "object" or "properties" in out:
            out["additionalProperties"] = False
        if "oneOf" in out:                                    # structured outputs accept anyOf, not oneOf
            out["anyOf"] = out.pop("oneOf")
        if notes:
            out["description"] = (out.get("description", "") + f" ({'; '.join(notes)})").strip()
        return out

    return walk(schema)


def fix_enums(data, schema):
    """Structured outputs may return an enum value in a different letter case: map it back to the schema's."""
    if isinstance(data, dict) and isinstance(schema.get("properties"), dict):
        return {k: fix_enums(v, schema["properties"].get(k, {})) for k, v in data.items()}
    if isinstance(data, list) and isinstance(schema.get("items"), dict):
        return [fix_enums(v, schema["items"]) for v in data]
    if isinstance(data, str):
        options = list(schema.get("enum", []))
        for alt in schema.get("anyOf", []):
            options += alt.get("enum", [])
        for o in options:
            if isinstance(o, str) and o.lower() == data.lower():
                return o
    return data


def to_messages(messages):
    """LangChain-style prompts ([("system", text), ("human", text)], or role dicts) -> (system, messages)."""
    system, out = [], []
    for m in messages:
        if isinstance(m, tuple):
            role, content = m
        elif isinstance(m, dict):
            role, content = m["role"], m["content"]
        else:                                                 # a LangChain message object
            role, content = getattr(m, "type", "human"), m.content
        role = {"human": "user", "ai": "assistant"}.get(role, role)
        if role == "system":
            system.append(content if isinstance(content, str) else json.dumps(content))
            continue
        if out and out[-1]["role"] == role and isinstance(content, str) and isinstance(out[-1]["content"], str):
            out[-1]["content"] += "\n\n" + content            # the API wants alternating roles
        else:
            out.append({"role": role, "content": content})
    return "\n\n".join(system), out


def tool_def(tool):
    """A LangChain StructuredTool -> an Anthropic tool definition."""
    schema = tool.args_schema.model_json_schema() if hasattr(tool.args_schema, "model_json_schema") else tool.args_schema
    return {"name": tool.name, "description": tool.description or tool.name, "input_schema": strict_schema(schema)}


def usage(resp):
    """The response's usage in the shape model.price() reads (input includes cached and cache-written tokens)."""
    u = resp.get("usage") or {}
    read, write = int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0)
    thinking = int((u.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
    return {"input_tokens": int(u.get("input_tokens") or 0) + read + write, "output_tokens": int(u.get("output_tokens") or 0),
            "input_token_details": {"cache_read": read, "cache_creation": write},
            "output_token_details": {"reasoning": thinking}}


def parse_json(resp):
    """The structured answer from a response, or ValueError naming why there is none."""
    stop = resp.get("stop_reason")
    if stop == "refusal":
        raise ValueError("the model refused to answer")
    if stop == "max_tokens":
        raise ValueError("the answer was cut off at max_output_tokens; raise it in agents/config.yaml")
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()
    if not text:
        raise ValueError(f"no answer text (stop_reason {stop})")
    return json.loads(text)


PROVIDERS = ("anthropic", "anthropic_aws", "bedrock_anthropic")


def credentials(m):
    """How this model authenticates: ("key", key) or ("sigv4", (access_key, secret_key, token)); None when
    nothing is set. Inside a sandbox the gateway adds the key, so a placeholder is used there."""
    from agents.common import sigv4
    key = os.environ.get(m["api_key_env"]) if m.get("api_key_env") else None
    if key:
        return "key", key
    if m["provider"] in ("anthropic_aws", "bedrock_anthropic"):
        aws = sigv4.env_credentials()
        if aws:
            return "sigv4", aws
    return None


def missing(m):
    """What is missing before this model can be called, or None."""
    if credentials(m) is None:
        if m["provider"] == "anthropic":
            return f"{m['api_key_env']} is empty in .env"
        return f"{m['api_key_env']} (or AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY) is empty in .env"
    if m["provider"] == "anthropic_aws" and not os.environ.get(m.get("workspace_env") or "ANTHROPIC_AWS_WORKSPACE_ID"):
        return f"{m.get('workspace_env') or 'ANTHROPIC_AWS_WORKSPACE_ID'} (wrkspc_...) is empty in .env"
    return None


class Claude:
    """One model's settings (cfg["model"], resolved by config.load) and the HTTP calls.

    provider "anthropic":         POST https://api.anthropic.com/v1/messages, header x-api-key.
    provider "anthropic_aws":     Claude Platform on AWS, POST https://aws-external-anthropic.{region}.api.aws/v1/messages,
                                  header anthropic-workspace-id, and x-api-key (a key from the AWS console's Claude
                                  Platform page) or SigV4 with AWS access keys (service aws-external-anthropic).
    provider "bedrock_anthropic": Amazon Bedrock, POST {bedrock-runtime}/model/{model}/invoke with the same Messages
                                  body (anthropic_version instead of model), Bearer Bedrock API key or SigV4 (bedrock).
    """

    def __init__(self, m, api_key=None):
        if m["provider"] not in PROVIDERS:
            raise ValueError(f"not a Claude provider: {m['provider']}")
        self.m = m
        self.auth = ("key", api_key) if api_key else credentials(m)
        self.http = httpx2.Client(timeout=httpx2.Timeout(30, read=m["timeout_s"]))

    @property
    def model_id(self):
        return self.m.get("model_id") or self.m["id"]

    def url(self):
        m = self.m
        if m["provider"] == "bedrock_anthropic":
            return f"{m['base_url'].rstrip('/')}/model/{quote(self.model_id, safe='')}/invoke"
        return f"{m['base_url'].rstrip('/')}/v1/messages"

    def create(self, system, messages, *, tools=None, tool_choice=None, schema=None):
        m = self.m
        body = {"max_tokens": int(m["max_output_tokens"]), "messages": messages}
        if m["provider"] == "bedrock_anthropic":
            body["anthropic_version"] = "bedrock-2023-05-31"
        else:
            body["model"] = self.model_id
        if system:
            body["system"] = system
        if m.get("thinking"):
            body["thinking"] = {"type": m["thinking"]}
        cfg = {}
        if m.get("effort"):
            cfg["effort"] = m["effort"]
        if schema is not None:
            cfg["format"] = {"type": "json_schema", "schema": strict_schema(schema)}
        if cfg:
            body["output_config"] = cfg
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or {"type": "auto"}
        return self._post(body)

    def _headers(self, url, raw):
        m = self.m
        h = {"content-type": "application/json", "accept": "application/json"}
        if m["provider"] != "bedrock_anthropic":
            h["anthropic-version"] = VERSION
        if m["provider"] == "anthropic_aws":
            h["anthropic-workspace-id"] = os.environ.get(m.get("workspace_env") or "ANTHROPIC_AWS_WORKSPACE_ID", "")
        if self.auth is None:
            raise APIError(0, "no_credentials", missing(m) or "no key")
        kind, cred = self.auth
        if kind == "key":
            if m["provider"] == "bedrock_anthropic":
                h["authorization"] = f"Bearer {cred}"
            else:
                h["x-api-key"] = cred
            return h
        from agents.common import sigv4
        service = "bedrock" if m["provider"] == "bedrock_anthropic" else "aws-external-anthropic"
        access, secret, token = cred
        h.update(sigv4.sign("POST", url, h, raw, access_key=access, secret_key=secret, session_token=token,
                            region=m.get("region") or "us-east-2", service=service))
        return h

    def _post(self, body):
        url = self.url()
        raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
        retries = int(self.m.get("max_retries", 6))
        for attempt in range(retries + 1):
            headers = self._headers(url, raw)                 # signed again each try (the date changes)
            try:
                r = self.http.post(url, content=raw, headers=headers)
            except (httpx2.TimeoutException, httpx2.TransportError):
                if attempt >= retries:
                    raise
                time.sleep(min(30.0, 2 ** attempt) + random.random())
                continue
            if r.status_code < 400:
                return r.json()
            try:
                data = r.json()
            except ValueError:
                data = {}
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            kind = err.get("type") or r.headers.get("x-amzn-errortype", "error").split(":")[0]
            message = err.get("message") or data.get("message") or data.get("Message") or r.text[:300]
            if r.status_code in RETRY_STATUS and attempt < retries:
                wait = r.headers.get("retry-after")
                time.sleep(min(60.0, float(wait)) if wait and wait.replace(".", "", 1).isdigit()
                           else min(30.0, 2 ** attempt) + random.random())
                continue
            raise APIError(r.status_code, kind, message)
        raise APIError(0, "retries", "no answer after retries")
