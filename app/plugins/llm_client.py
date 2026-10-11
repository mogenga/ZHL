"""LLMClient Protocol + MockLLMClient (PA plan Task 3 / PA spec §5.7, R4).

``LLMClient`` is the pluggable-model contract pinned by 总 spec §9.1:
a single ``chat(prompt, schema) -> dict`` call. Phase A ships only the
deterministic ``MockLLMClient`` — it reads a fixed JSON fixture
(``tests/fixtures/mock_llm_outputs.json``, P0-frozen) and does NO network
I/O and reads NO ``ZHL_LLM_*`` env vars (R4: those are P1-only).

Routing rule (PA spec §7 B1): the caller embeds a stable fixture key in
the prompt as a ``[SOURCE_SN:<sn>]`` marker; ``MockLLMClient`` parses that
marker, looks the key up in the fixture's ``extraction`` block
(``source_sn`` field), and returns the block verbatim. A missing marker
or an unmatched key both raise ``KeyError`` so failures stay loud and
deterministic (no silent fallback, no invented output).

Purity boundary: no ``requests`` / ``httpx`` / ``os.environ`` / clock.
Phase 1 swaps in a real client by implementing the same Protocol — the
extract Skill only ever talks to ``LLMClient``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

_SOURCE_SN_MARKER = re.compile(r"\[SOURCE_SN:([^\]]+)\]")


@runtime_checkable
class LLMClient(Protocol):
    """Pluggable model-call contract (总 spec §9.1 / §340)."""

    def chat(self, prompt: str, schema: dict) -> dict:
        """One structured chat completion; returns a plain dict."""
        ...


class MockLLMClient:
    """Deterministic fixture-backed LLMClient (PA spec §5.7, R4).

    ``fixture_dir`` is the directory containing ``mock_llm_outputs.json``
    (the P0-frozen ``tests/fixtures`` tree in-repo; injectable for tests).
    Construction is eager: a missing/malformed fixture fails fast at init,
    not mid-workflow.
    """

    def __init__(self, fixture_dir: Path) -> None:
        fixture_path = Path(fixture_dir) / "mock_llm_outputs.json"
        raw = fixture_path.read_text(encoding="utf-8")
        self._fixture = json.loads(raw)

    def chat(self, prompt: str, schema: dict) -> dict:
        """Route ``prompt`` to the fixture's ``extraction`` block.

        The marker ``[SOURCE_SN:<sn>]`` (B1) is parsed out of the prompt;
        the fixture block whose ``source_sn`` equals ``<sn>`` is returned
        verbatim. Missing marker or unmatched key -> ``KeyError``.
        ``schema`` is accepted for Protocol compatibility; the mock is
        schema-agnostic (fixture is the schema's source of truth).
        """
        match = _SOURCE_SN_MARKER.search(prompt)
        if match is None:
            raise KeyError(
                "no [SOURCE_SN:<sn>] fixture key found in prompt — "
                "the extract Skill must embed the marker per PA spec B1"
            )
        source_sn = match.group(1)
        block = self._fixture.get("extraction")
        if not isinstance(block, dict) or block.get("source_sn") != source_sn:
            raise KeyError(f"no fixture extraction block for source_sn={source_sn!r}")
        return block
