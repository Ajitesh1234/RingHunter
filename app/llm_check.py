"""Run:  python -m app.llm_check   - tests your LLM key/model without touching the database."""
from . import llm
from .config import settings

print("provider:", llm.provider(), "| model:", llm.model_name())
if llm.provider() == "offline":
    raise SystemExit("No key loaded. Check .env (GROQ_API_KEY=...) and restart.")
try:
    print("plain call ->", llm.complete("Reply with exactly: OK", "ping", 10).strip()[:40])
except Exception as e:
    print("plain call FAILED:", e)
tools = [{"name": "echo", "description": "Echo a word", "parameters": {"type": "object",
         "properties": {"word": {"type": "string"}}, "required": ["word"]}}]
try:
    out = llm.run_agent("Call the echo tool with word=hi, then reply DONE.", "go", tools, lambda n, a: {"echoed": a}, max_turns=3, max_tokens=100)
    print("tool call ->", out.strip()[:40])
except Exception as e:
    print("tool call FAILED:", e)
