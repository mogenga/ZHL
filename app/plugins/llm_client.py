"""LLMClient Protocol + Mock / OpenAI-compatible implementations (PB Task 3, L1/L1a).

总 spec §9.1: the model is *not* the system of record. The only model I/O
channel is this Protocol; every call is wrapped in a Skill, leaves a trace,
and carries schema-constrained output. Phase B pins the attribution use case
with a deterministic ``MockLLMClient`` so no live model is required in tests
(O-2 assumption: no historical data; attribution verified via mock).

MERGED 2026-10-12 (union, Phase A extraction + Phase B attribution): this module now
serves BOTH routing strategies on one MockLLMClient — [SOURCE_SN:<sn>] -> frozen
mock_llm_outputs.json extraction block (Phase A); incident_id -> PB-new
mock_llm_attribution.json hypothesis dict (Phase B). See
docs/superpowers/plans/2026-10-12-phaseb-merge-conflicts.md (conflict 2).

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

# Phase A extraction marker (PA spec B1): [SOURCE_SN:<sn>] -> frozen
# mock_llm_outputs.json extraction block (union with Phase B routing).
_SOURCE_SN_RE = re.compile(r"\[SOURCE_SN:([^\]]+)\]")

# P0-frozen extraction fixture vs PB-new attribution fixture.
_EXTRACT_FIXTURE = "mock_llm_outputs.json"
_ATTRIBUTION_FIXTURE = "mock_llm_attribution.json"


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
    """Deterministic fixture-backed LLMClient (PB L1a + PA Task 3, union).

    Union (2026-10-12 merge) — one class, two routing strategies:
    - Phase A extraction: ``[SOURCE_SN:<sn>]`` marker in the prompt routes to
      the P0-frozen ``mock_llm_outputs.json`` ``extraction`` block.
    - Phase B attribution: ``incident_id`` regex routes to the PB-new
      ``mock_llm_attribution.json`` hypothesis dict.

    ``fixture_path`` accepts either a **directory** (Phase A form, loads
    both fixtures if present) or a **file** (Phase B form, loads that
    attribution fixture). Unknown markers/ids raise ``KeyError`` — never a
    silent empty response.
    """

    def __init__(self, fixture_path: str | Path) -> None:
        path = Path(fixture_path)
        self._extract_fixtures: dict = {}
        self._attribution: dict = {}
        if path.is_dir():
            # Phase A form: a fixtures directory (e.g. tests/fixtures).
            extract_file = path / _EXTRACT_FIXTURE
            if not extract_file.is_file():
                raise FileNotFoundError(f"extraction fixture not found: {extract_file}")
            self._extract_fixtures = json.loads(extract_file.read_text(encoding="utf-8"))
            attr_file = path / _ATTRIBUTION_FIXTURE
            if attr_file.is_file():
                self._attribution = json.loads(attr_file.read_text(encoding="utf-8"))
        elif path.is_file():
            # Phase B form: a direct attribution fixture file.
            self._attribution = json.loads(path.read_text(encoding="utf-8"))
        else:
            raise FileNotFoundError(f"LLM fixture not found: {path}")

    def chat(self, prompt: str, schema: dict) -> dict:
        # Phase A extraction marker wins when present.
        m_sn = _SOURCE_SN_RE.search(prompt)
        if m_sn is not None:
            source_sn = m_sn.group(1)
            block = self._extract_fixtures.get("extraction")
            if not isinstance(block, dict) or block.get("source_sn") != source_sn:
                raise KeyError(f"no fixture extraction block for source_sn={source_sn!r}")
            return block
        # Phase B attribution routing.
        m_inc = _INCIDENT_ID_RE.search(prompt)
        if m_inc is not None:
            incident_id = m_inc.group(1)
            if incident_id in self._attribution:
                return self._attribution[incident_id]
        if m_inc is None:
            raise KeyError("prompt carries neither a [SOURCE_SN] marker nor an incident_id")
        raise KeyError(f"no mock attribution for incident_id={m_inc.group(1)!r}")


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
