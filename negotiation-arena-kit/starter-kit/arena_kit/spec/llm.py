"""Model calls with hard timeouts: a hung call never holds the case (R10.2).

`from_env(timeout)` gives `complete(system, user, timeout=None) -> str` on LLM_PROVIDER (as the kit's
`llm_from_env`); each call runs in a worker thread and is abandoned at its deadline. `SPEC_LLM=0` (or
no credentials) switches the model off - the agent then writes every e-mail by code.
"""

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

import httpx

log = logging.getLogger("arena")
POOL = ThreadPoolExecutor(max_workers=32, thread_name_prefix="spec-llm")


def parse_json(text):
    """The first JSON object in a model reply (tolerates code fences and chatter)."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise ValueError(f"no JSON in reply: {str(text)[:200]}")
    return json.loads(match.group(0))


def with_deadline(fn, timeout, *args):
    """fn(*args) in a worker thread; TimeoutError after `timeout` seconds (the call is abandoned)."""
    future = POOL.submit(fn, *args)
    try:
        return future.result(timeout=max(0.1, timeout))
    except FuturesTimeout:
        future.cancel()
        raise TimeoutError(f"model call over {timeout:.0f}s") from None


def from_env(default_timeout=35.0):
    """A `complete(system, user, timeout=None)` on the configured provider, or None (no model)."""
    # code-written e-mails by default: code-only scored 0.733 on the practice cases, with the model 0.661
    # (it misread replies: P08 a payment ask into our sheets, P01 the earliest date). SPEC_LLM=1 turns it on.
    if os.environ.get("SPEC_LLM", "0").lower() in ("0", "false", "no", "off"):
        return None
    provider, model = os.environ.get("LLM_PROVIDER", "openai"), os.environ.get("LLM_MODEL")
    if not model:
        log.warning("spec agent: no LLM_MODEL - code-written e-mails")
        return None
    if provider == "anthropic":
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            log.warning("spec agent: no Anthropic credentials - code-written e-mails")
            return None
        import anthropic

        client = anthropic.Anthropic(max_retries=0)
        extra = {"output_config": {"effort": e}} if (e := os.environ.get("LLM_EFFORT")) else {}

        def call(system, user, timeout):
            r = client.messages.create(
                model=model, max_tokens=4000, system=system, timeout=timeout,
                messages=[{"role": "user", "content": user}], **extra,
            )  # fmt: skip
            return "".join(b.text for b in r.content if b.type == "text")

    else:
        key = os.environ.get("LLM_API_KEY")
        if not key:
            log.warning("spec agent: no LLM_API_KEY - code-written e-mails")
            return None
        base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        http = httpx.Client(headers={"Authorization": f"Bearer {key}"})

        def call(system, user, timeout):
            body = {
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_object"},
            }
            r = http.post(f"{base}/chat/completions", json=body, timeout=timeout)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    def complete(system, user, timeout=None):
        t = timeout or default_timeout
        return with_deadline(call, t + 1.0, system, user, t)

    return complete
