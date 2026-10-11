"""LLMClient Protocol + Mock / OpenAI-compatible implementations (PB Task 3, L1/L1a).

总 spec §9.1: the model is *not* the system of record. The only model I/O
channel is this Protocol; every call is wrapped in a Skill, leaves a trace,
and carries schema-constrained output. Phase B pins the attribution use case
with a deterministic ``MockLLMClient`` so no live model is required in tests
(O-2 assumption: no historical data; attribution verified via mock).

Purity boundary (AGENT.md §8): this module lives in ``app/plugins`` and MAY
import ``httpx`` / stdlib, but never ``langgraph`` or model services beyond
the OpenAI-compatible HTTP endpoint defined here. Clock reads are forbidden
(trace determinism is the Skill/TraceStore's job, not the client's).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

# Prompt-contract key: the attribution Skill embeds the incident id in the
# prompt so the mock can route deterministically. The regex accepts both the
# quoted form (``"incident_id": "INC-001"``) and the bare form.
_INCIDENT_ID_RE = re.compile(r'["\']?incident_id["\']?\s*[:=]\s*["\']?([A-Za-z0-9_-]+)')


@runtime_checkable
class LLMClient(Protocol):
    """The model I/O contract (总 spec §9.1 / AGENT.md §6.2)."""

    def chat(self, prompt: str, schema: dict) -> dict:
        """Return a structured JSON object conforming to ``schema``.

        ``schema`` is the declarative output contract (key names / types the
        caller will validate). Implementations MUST NOT read the clock or
        mutate global state.
        """
        ...


class MockLLMClient:
    """Deterministic fixture-backed LLMClient (PB L1a).

    Routes by ``incident_id`` extracted from the prompt (via regex, so the
    prompt may be a natural-language description that merely *contains* the
    ``"incident_id": "<id>"`` substring). Unknown ids raise ``KeyError`` —
    never a silent empty response.

    The fixture file ``tests/fixtures/mock_llm_attribution.json`` is *new*
    (PB L1a); the Phase-0-frozen ``tests/fixtures/mock_llm_outputs.json`` is
    NOT modified (AGENT.md §4.2 Phase B non-goal + spec §2 Non-goals).
    """

    def __init__(self, fixture_path: str | Path) -> None:
        path = Path(fixture_path)
        if not path.is_file():
            raise FileNotFoundError(f"attribution fixture not found: {path}")
        self._fixture = json.loads(path.read_text(encoding="utf-8"))

    def chat(self, prompt: str, schema: dict) -> dict:
        m = _INCIDENT_ID_RE.search(prompt)
        if m is None:
            raise KeyError("prompt does not contain an incident_id for routing")
        incident_id = m.group(1)
        try:
            return self._fixture[incident_id]
        except KeyError:
            raise KeyError(f"no mock attribution for incident_id={incident_id!r}") from None


class OpenAICompatClient:
    """OpenAI-compatible chat client via httpx (PB L1, prod / demo path).

    Reads ``ZHL_LLM_API_KEY`` / ``ZHL_LLM_BASE_URL`` / ``ZHL_LLM_MODEL`` when
    the corresponding constructor arg is omitted (总 spec §9.1 env contract).
    The request body uses ``response_format={"type": "json_object"}`` so the
    model must emit a JSON object matching the caller's schema.

    Constructibility is validated eagerly: an empty/missing ``api_key`` raises
    ``ValueError`` (no silent no-op calls in production).
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("ZHL_LLM_API_KEY", "")
        self.base_url = (base_url or os.environ.get("ZHL_LLM_BASE_URL", "")).rstrip("/")
        self.model = model or os.environ.get("ZHL_LLM_MODEL", "")
        self.timeout_s = timeout_s
        if not self.api_key:
            raise ValueError("OpenAICompatClient requires an api_key (arg or ZHL_LLM_API_KEY env)")

    def chat(self, prompt: str, schema: dict) -> dict:
        import httpx  # deferred: keep the module importable without the dep at construction time

        body = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": prompt},
            ],
            # Structural-constraint hint (总 spec §9.1 D4; schema itself lives
            # in the prompt body the Skill assembles — the client stays generic).
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        with httpx.Client(timeout=self.timeout_s) as client:
            resp = client.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers=headers,
            )
            resp.raise_for_status()
        data = resp.json()
        # OpenAI-compatible response: choices[0].message.content is a JSON string.
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError(f"LLM returned non-object JSON: {type(parsed).__name__}")
        return parsed
