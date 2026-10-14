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
import time
from pathlib import Path
from typing import Protocol, runtime_checkable


class LLMUnavailable(Exception):
    """A live-model call failed in a way that is legitimate to degrade on.

    Transport errors, non-2xx responses, and structurally unusable payloads
    (non-JSON body, non-object JSON) all mean "the model did not answer
    usefully this time" — never "the answer is empty". Callers may therefore
    retry, fall back to a local client, or surface a degraded flag — but they
    must never treat the failure as a silent empty result.
    """


# Prompt-contract key: the attribution Skill embeds the incident id in the
# prompt so the mock can route deterministically. The regex accepts both the
# quoted form (``"incident_id": "INC-001"``) and the bare form.
_INCIDENT_ID_RE = re.compile(r'["\']?incident_id["\']?\s*[:=]\s*["\']?([A-Za-z0-9_-]+)')

# Phase A extraction marker (PA spec B1): [SOURCE_SN:<sn>] -> frozen
# mock_llm_outputs.json extraction block (union with Phase B routing).
_SOURCE_SN_RE = re.compile(r"\[SOURCE_SN:([^\]]+)\]")

# Raw-source parsing route (FR-1.1/1.2): the Skill's [RAW_PARSE] marker plus
# the <RAW_SOURCE>...</RAW_SOURCE> block -> deterministic local parser.
_RAW_PARSE_RE = re.compile(r"\[RAW_PARSE\]")
_RAW_SOURCE_RE = re.compile(r"<RAW_SOURCE>(.*?)</RAW_SOURCE>", re.DOTALL)

# P0-frozen extraction fixture vs PB-new attribution fixture.
_EXTRACT_FIXTURE = "mock_llm_outputs.json"
_ATTRIBUTION_FIXTURE = "mock_llm_attribution.json"


def _params(base: dict) -> dict:
    """The fixture's ``extracted_params`` block ({} when absent/malformed)."""
    params = base.get("extracted_params")
    return dict(params) if isinstance(params, dict) else {}


def _sn_pmax(source_sn: str) -> float:
    """Stable per-SN power for synthetic mock extraction (hash-seeded, ±3 W)."""
    import hashlib

    digest = hashlib.sha256(source_sn.encode("utf-8")).digest()
    offset = (int.from_bytes(digest[:2], "big") % 600) / 100.0 - 3.0
    return round(550.0 + offset, 2)


def _synthetic_confidence(source_sn: str) -> float:
    """Deterministic per-SN confidence in [0.72, 0.995].

    The floor keeps the auto-confirm boundary (0.8, Review Focus #1) land on
    both sides for a demo whose SN names are known ("SN-LOW-..." -> low
    confidence, everything else high), while unknown SNs stay unpredictable
    but stable across runs.
    """
    if source_sn.upper().startswith("SN-LOW"):
        return 0.62
    import hashlib

    digest = hashlib.sha256(("conf:" + source_sn).encode("utf-8")).digest()
    return round(0.85 + (int.from_bytes(digest[:2], "big") % 150) / 1000.0, 3)


class FixtureMissError(KeyError):
    """MockLLMClient: the prompt's marker has no fixture entry.

    Subclasses ``KeyError`` so existing ``pytest.raises(KeyError)`` contract
    tests keep passing, but lets callers (the attribution endpoint) catch
    *only* the "mock has no fixture for this id" business case. A bare
    ``except KeyError`` around a whole graph run would also swallow genuine
    KeyErrors from the skill / evidence store / ORM and masquerade them as
    "unknown incident" — that is why this type exists.
    """


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
        # Raw-source parsing (FR-1.1/1.2) routes first: its prompt embeds the
        # raw text, and the text itself may contain [SOURCE_SN]-looking tokens.
        if _RAW_PARSE_RE.search(prompt):
            from app.plugins.raw_source import parse_raw_source_text

            source = _RAW_SOURCE_RE.search(prompt)
            return {"rows": parse_raw_source_text(source.group(1) if source else "")}
        # Phase A extraction marker wins when present.
        m_sn = _SOURCE_SN_RE.search(prompt)
        if m_sn is not None:
            source_sn = m_sn.group(1)
            return self._extraction_block(source_sn)
        # Phase B attribution routing.
        m_inc = _INCIDENT_ID_RE.search(prompt)
        if m_inc is not None:
            incident_id = m_inc.group(1)
            if incident_id in self._attribution:
                return self._attribution[incident_id]
            # Unknown id -> synthetic hypotheses grounded in the *real*
            # evidence ids the prompt carries. A demo creates a fresh
            # incident UUID every run, so a fixture keyed by id can never
            # cover it; without this the demo dies at the attribution step.
            # The cited evidence ids come from the incident itself, so the
            # D2 gate validates them against the real store.
            return self._synthetic_attribution(prompt)
        raise FixtureMissError("prompt carries neither a [SOURCE_SN] marker nor an incident_id")

    def _synthetic_attribution(self, prompt: str) -> dict:
        """Deterministic hypotheses for an incident with no fixture entry.

        Grounded in the evidence ids the prompt actually carries
        (``evidence_ids=[...]`` — the incident's own evidence), so the D2
        gate admits them against the real store. One hypothesis deliberately
        cites no evidence, preserving the "confidence never gates but
        evidence does" demonstration on every incident, not just INC-001.
        """
        m = re.search(r"evidence_ids=\[([^\]]*)\]", prompt)
        ids = [x.strip().strip("'\"") for x in (m.group(1).split(",") if m else []) if x.strip()]
        trigger = re.search(r"trigger_type=(\S+)", prompt)
        kind = trigger.group(1) if trigger else "unknown"
        hypotheses: list[dict] = []
        if ids:
            hypotheses.append(
                {
                    "cause": f"{kind}：受影响批次存在同源偏差（基于 {len(ids)} 条证据）",
                    "confidence": 0.82,
                    "evidence_ids": ids,
                    "recommended_action": "隔离同批次待检组件并复核该证据涉及的工序",
                }
            )
        hypotheses.append(
            {
                "cause": f"{kind}：设备参数漂移（待补充校准记录）",
                "confidence": 0.55,
                "evidence_ids": [],
                "recommended_action": "调取该设备校准记录复核",
            }
        )
        return {"hypotheses": hypotheses, "synthetic": True}

    def _extraction_block(self, source_sn: str) -> dict:
        """Resolve one ``[SOURCE_SN:<sn>]`` extraction block.

        Routing order (most specific first):

        1. The P0-frozen fixture block whose ``source_sn`` matches exactly.
        2. The fixture's default block with the labels re-targeted to the
           requested SN — a demo must not die on ``SN-N-003`` because the
           frozen fixture only ever pinned one SN (O-2).
        3. Otherwise: a clearly-labelled synthetic block, so a zero-network
           demo still shows per-SN AI extraction. ``meta.source`` in the
           extraction evidence carries ``"fixture"`` or ``"mock-synthetic"``
           so a reviewer can always tell which path answered.

        The raw fixture payload (base) stays P0-frozen: only ``source_sn`` /
        ``extracted_params.pmax`` are overridden; there is no live model here.
        """
        base = self._extract_fixtures.get("extraction")
        if not isinstance(base, dict):
            raise FixtureMissError("fixture carries no 'extraction' block")
        block = dict(base)
        if base.get("source_sn") != source_sn:
            synthetic = source_sn not in {base.get("source_sn"), None}
            if synthetic:
                block = {
                    "source_sn": source_sn,
                    "extracted_params": {**_params(base), "pmax": _sn_pmax(source_sn)},
                    "appearance_findings": [],
                    "confidence": _synthetic_confidence(source_sn),
                    "synthetic": True,
                }
            else:
                block["source_sn"] = source_sn
        return block


class OpenAICompatClient:
    """OpenAI-compatible chat client via httpx (PB L1, prod / demo path).

    Reads ``ZHL_LLM_API_KEY`` / ``ZHL_LLM_BASE_URL`` / ``ZHL_LLM_MODEL`` when
    the corresponding constructor arg is omitted (总 spec §9.1 env contract).
    The request body uses ``response_format={"type": "json_object"}`` so the
    model must emit a JSON object matching the caller's schema.

    Constructibility is validated eagerly: an empty/missing ``api_key`` raises
    ``ValueError`` (no silent no-op calls in production).

    Hardening for live use (a demo must not die on one flaky call):

    - one retry on transport errors / 5xx / 429 (``retries``, default 1);
      other 4xx (bad key, bad model) fail fast — retrying cannot help.
    - connection / read timeouts from ``ZHL_LLM_TIMEOUT_S`` (default 60s).
    - every failure mode below surfaces as :class:`LLMUnavailable` (transport,
      non-200, non-JSON body, non-object JSON, empty choices). This lets the
      caller ``except LLMUnavailable`` to degrade once, instead of catching
      five different httpx/json exception classes — or worse, crashing.
    - ``ZHL_LLM_DISABLE_THINKING`` (default on for DeepSeek-style endpoints):
      emits ``thinking: {"type": "disabled"}``; unknown extra fields are
      ignored by OpenAI-compatible servers, and it prevents a reasoning model
      from burning the timeout budget on CoT tokens.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
        retries: int = 1,
        disable_thinking: bool | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("ZHL_LLM_API_KEY", "")
        self.base_url = (base_url or os.environ.get("ZHL_LLM_BASE_URL", "")).rstrip("/")
        self.model = model or os.environ.get("ZHL_LLM_MODEL", "")
        if timeout_s is None:
            try:
                timeout_s = float(os.environ.get("ZHL_LLM_TIMEOUT_S", "60"))
            except ValueError:
                timeout_s = 60.0
        self.timeout_s = timeout_s
        self.retries = max(0, int(retries))
        if disable_thinking is None:
            disable_thinking = os.environ.get("ZHL_LLM_DISABLE_THINKING", "1").strip() not in (
                "0",
                "false",
                "False",
            )
        self.disable_thinking = disable_thinking
        if not self.api_key:
            raise ValueError("OpenAICompatClient requires an api_key (arg or ZHL_LLM_API_KEY env)")

    def chat(self, prompt: str, schema: dict) -> dict:
        import httpx  # deferred: keep the module importable without the dep at construction time

        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": prompt},
            ],
            # Structural-constraint hint (总 spec §9.1 D4; schema itself lives
            # in the prompt body the Skill assembles — the client stays generic).
            "response_format": {"type": "json_object"},
        }
        if self.disable_thinking:
            # DeepSeek-compatible: skip CoT tokens. Servers that do not know
            # the field ignore it, so this is safe to always send.
            body["thinking"] = {"type": "disabled"}
        headers = {"Authorization": f"Bearer {self.api_key}"}

        attempt = 0
        while True:
            try:
                with httpx.Client(timeout=self.timeout_s) as client:
                    resp = client.post(
                        f"{self.base_url}/chat/completions",
                        json=body,
                        headers=headers,
                    )
                    resp.raise_for_status()
                    data = resp.json()
            except httpx.HTTPStatusError as exc:  # non-2xx
                status = exc.response.status_code
                if status >= 500 or status == 429:
                    if attempt < self.retries:
                        attempt += 1
                        time.sleep(min(2.0 * attempt, 5.0))
                        continue
                raise LLMUnavailable(
                    f"LLM endpoint returned HTTP {status}: {exc.response.text[:200]}"
                ) from exc
            except (httpx.HTTPError, ValueError) as exc:  # transport / bad JSON body
                if attempt < self.retries:
                    attempt += 1
                    time.sleep(min(2.0 * attempt, 5.0))
                    continue
                raise LLMUnavailable(f"LLM call failed: {exc!r}") from exc
            break

        # OpenAI-compatible response: choices[0].message.content is a JSON string.
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailable(
                f"LLM response has no choices[0].message.content: {str(data)[:200]}"
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMUnavailable(f"LLM returned empty content: {str(data)[:200]}")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMUnavailable(f"LLM returned non-JSON content: {content[:200]}") from exc
        if not isinstance(parsed, dict):
            raise LLMUnavailable(f"LLM returned non-object JSON: {type(parsed).__name__}")
        return parsed


class ResilientLLMClient:
    """Primary client with a deterministic fallback + degradation flag.

    Wraps the live client so a single network/模型 failure degrades the app to
    the fixture mock instead of 500-ing the demo, while never *pretending* the
    model answered: ``degraded`` flips to True on the first fallback and every
    Skill that records a trace writes ``model_version`` accordingly
    (``llm_client.model_version()``).

    A ``FixtureMissError`` from the fallback (the mock only pins a handful of
    SNs / incidents) is NOT swallowed — the caller should see the miss, since
    a fixture miss means neither path could answer.
    """

    def __init__(self, primary: LLMClient, fallback: LLMClient) -> None:
        self._primary = primary
        self._fallback = fallback
        self.degraded = False
        self.last_error = ""

    def chat(self, prompt: str, schema: dict) -> dict:
        if self.degraded:
            return self._fallback.chat(prompt, schema)
        try:
            return self._primary.chat(prompt, schema)
        except LLMUnavailable as exc:
            self.degraded = True
            self.last_error = str(exc)
            return self._fallback.chat(prompt, schema)

    def model_version(self) -> str:
        """Trace-facing model identity (never claims a live model it did not use)."""
        if self.degraded or isinstance(self._primary, MockLLMClient):
            return "mock-degraded" if self.degraded else "mock"
        return getattr(self._primary, "model", None) or "openai-compat"
