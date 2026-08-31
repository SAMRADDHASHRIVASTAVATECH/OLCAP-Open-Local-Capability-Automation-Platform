"""
Model bridge.

MODEL POLICY: model selection and routing stay with OpenCode
(OpenCode model selection -> Google API or Groq API -> selected model).

This bridge is only used when the Unified Core needs a *model* for a small
decision (synthesis, summarisation, planning assistance). It never becomes a
second mandatory gateway, never silently replaces OpenCode's routing, and
never requires local inference. If no external backend is reachable it falls
back to a deterministic extractive implementation.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from .config import cfg
from .observability import span


class LLMUnavailable(RuntimeError):
    pass


class ModelBridge:
    def __init__(self) -> None:
        self.c = cfg()

    # ------------------------------------------------------------------ #
    def backend(self) -> str:
        forced = self.c.env("LLM_BACKEND")
        if forced:
            return forced
        if self.c.has("opencode"):
            return "opencode"
        if self.c.env("GOOGLE_API_KEY") or self.c.env("GEMINI_API_KEY"):
            return "google"
        if self.c.env("GROQ_API_KEY"):
            return "groq"
        return "stub"

    # ------------------------------------------------------------------ #
    def complete(self, prompt: str, system: str = "", max_tokens: int = 1200,
                 temperature: float = 0.2, json_mode: bool = False) -> Dict[str, Any]:
        backend = self.backend()
        with span("llm.complete", "model", {"backend": backend,
                                            "chars": len(prompt)}) as sp:
            try:
                if backend == "opencode":
                    out = self._opencode(prompt, system)
                elif backend == "google":
                    out = self._google(prompt, system, max_tokens, temperature)
                elif backend == "groq":
                    out = self._groq(prompt, system, max_tokens, temperature)
                else:
                    out = self._stub(prompt, system)
            except Exception as e:
                sp.set(error=str(e)[:200])
                out = {"text": self._stub(prompt, system)["text"],
                       "backend": "stub_fallback",
                       "degraded": True, "error": str(e)[:200]}
            sp.set(backend=out.get("backend"))
            return out

    # ------------------------------------------------------------------ #
    def _opencode(self, prompt: str, system: str) -> Dict[str, Any]:
        """Use the OpenCode CLI so the model choice stays inside OpenCode."""
        import subprocess
        model = self.c.env("OPENCODE_MODEL", "")
        cmd = ["opencode", "run", "--print-logs"]
        if model:
            cmd += ["--model", model]
        cmd += [f"{system}\n\n{prompt}" if system else prompt]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=int(self.c.env("LLM_TIMEOUT_S", "180")))
        if r.returncode != 0:
            raise LLMUnavailable(f"opencode rc={r.returncode}: {r.stderr[:200]}")
        return {"text": r.stdout, "backend": "opencode", "model": model or "opencode-default"}

    def _google(self, prompt: str, system: str, max_tokens: int, temperature: float
                ) -> Dict[str, Any]:
        import json as _json
        import urllib.request
        key = self.c.env("GOOGLE_API_KEY") or self.c.env("GEMINI_API_KEY")
        model = self.c.env("GOOGLE_MODEL", "gemini-2.0-flash")
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}:"
               f"generateContent?key={key}")
        body = {"contents": [{"parts": [{"text": (system + "\n\n" + prompt) if system
                                                 else prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens,
                                     "temperature": temperature}}
        req = urllib.request.Request(url, data=_json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = _json.loads(r.read().decode())
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return {"text": text, "backend": "google", "model": model}

    def _groq(self, prompt: str, system: str, max_tokens: int, temperature: float
              ) -> Dict[str, Any]:
        import json as _json
        import urllib.request
        key = self.c.env("GROQ_API_KEY")
        model = self.c.env("GROQ_MODEL", "llama-3.3-70b-versatile")
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=_json.dumps({"model": model,
                              "messages": ([{"role": "system", "content": system}]
                                           if system else []) +
                                          [{"role": "user", "content": prompt}],
                              "max_tokens": max_tokens,
                              "temperature": temperature}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = _json.loads(r.read().decode())
        return {"text": data["choices"][0]["message"]["content"],
                "backend": "groq", "model": model}

    # ------------------------------------------------------------------ #
    def _stub(self, prompt: str, system: str) -> Dict[str, Any]:
        """
        Deterministic extractive fallback. Not a model - it never invents facts:
        it compresses the material already present in the prompt.
        """
        lines = [l.strip() for l in prompt.splitlines() if l.strip()]
        body = [l for l in lines if len(l) > 40]
        if not body:
            body = lines
        sentences: List[str] = []
        for l in body[:40]:
            sentences.extend(s for s in re.split(r"(?<=[.!?])\s+", l) if len(s) > 25)
        scored = sorted(sentences, key=lambda s: -self._score(s))[:8]
        text = "\n".join(f"- {s}" for s in scored) if scored else "(no material supplied)"
        return {"text": text, "backend": "stub", "degraded": True,
                "note": "deterministic extractive fallback; no external model used"}

    @staticmethod
    def _score(s: str) -> float:
        terms = ("because", "however", "therefore", "result", "shows", "found",
                 "according", "report", "study", "data", "increase", "decrease",
                 "requires", "must", "provides", "supports")
        return sum(1 for t in terms if t in s.lower()) + min(len(s) / 200.0, 1.0)


_BRIDGE: Optional[ModelBridge] = None


def llm() -> ModelBridge:
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = ModelBridge()
    return _BRIDGE
