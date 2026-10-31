"""One small LLM layer. Free-tier friendly: Groq (OpenAI-compatible) or Anthropic, or offline if no key."""
import json
import httpx
from .config import settings

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def provider() -> str:
    if settings.groq_key:
        return "groq"
    if settings.anthropic_key:
        return "anthropic"
    return "offline"


def model_name() -> str:
    return {"groq": settings.groq_model, "anthropic": settings.anthropic_model}.get(provider(), "offline")


def _groq(payload: dict) -> dict:
    extra = {"reasoning_effort": "low"} if settings.groq_model.startswith("openai/gpt-oss") else {}
    r = httpx.post(GROQ_URL, headers={"Authorization": f"Bearer {settings.groq_key}"},
                   json={"model": settings.groq_model, **extra, **payload}, timeout=90)
    if r.status_code >= 400:  # surface Groq's own message (never contains our key)
        raise RuntimeError(f"Groq HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def complete(system: str, user: str, max_tokens: int = 500, json_mode: bool = False) -> str:
    if provider() == "groq":
        extra = {"response_format": {"type": "json_object"}} if json_mode else {}
        d = _groq({"max_tokens": max_tokens, "temperature": 0.2, **extra,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        return d["choices"][0]["message"]["content"] or ""
    import anthropic
    r = anthropic.Anthropic(api_key=settings.anthropic_key).messages.create(
        model=settings.anthropic_model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in r.content if b.type == "text")


def run_agent(system: str, user: str, tools: list[dict], tool_fn, max_turns: int = 4, max_tokens: int = 2000) -> str:
    """Tool-use loop. tools = [{name, description, parameters(JSON schema)}]. tool_fn(name, args) -> JSON-able."""
    if provider() == "groq":
        spec = [{"type": "function", "function": t} for t in tools]
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for _ in range(max_turns):
            m = _groq({"messages": msgs, "tools": spec, "tool_choice": "auto", "max_tokens": max_tokens,
                       "temperature": 0.2})["choices"][0]["message"]
            if m.get("tool_calls"):
                msgs.append({"role": "assistant", "content": m.get("content"), "tool_calls": m["tool_calls"]})
                for tc in m["tool_calls"]:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                    msgs.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": json.dumps(tool_fn(tc["function"]["name"], args))})
                continue
            return m.get("content") or ""
        msgs.append({"role": "user", "content": "Stop calling tools and reply now with the final JSON only."})
        return _groq({"messages": msgs, "max_tokens": max_tokens, "temperature": 0.2})["choices"][0]["message"]["content"] or ""
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_key)
    spec = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]
    msgs = [{"role": "user", "content": user}]
    for _ in range(max_turns + 1):
        r = client.messages.create(model=settings.anthropic_model, max_tokens=max_tokens, system=system, tools=spec, messages=msgs)
        if r.stop_reason == "tool_use":
            msgs.append({"role": "assistant", "content": r.content})
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(tool_fn(b.name, b.input))}
                for b in r.content if b.type == "tool_use"]})
            continue
        return "".join(b.text for b in r.content if b.type == "text")
    return ""
