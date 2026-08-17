"""외부 전적 검증 데이터의 안전한 결합 계층.

실시간 웹 스크래핑 결과를 임의로 만들지 않고, 운영자가 검증해 등록한 JSON 기록만
Riot Match-V5 결과와 결합한다. 파일은 EXTERNAL_SCOUTING_JSON 환경변수로 지정한다.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SOURCE_LABELS = {
    "opgg": "OP.GG",
    "yourgg": "YOUR.GG",
    "lolps": "LOL.PS",
    "fow": "FOW.LOL",
}


@dataclass(frozen=True)
class SourceEvidence:
    source: str
    status: str
    summary: str = ""
    url: str = ""
    verified_at: str = ""


@dataclass(frozen=True)
class EvidenceBundle:
    riot_live: bool = True
    external: tuple[SourceEvidence, ...] = field(default_factory=tuple)

    @property
    def verified_count(self) -> int:
        return sum(1 for item in self.external if item.status == "verified")

    def display_line(self) -> str:
        external = ", ".join(
            f"{item.source} {('확인' if item.status == 'verified' else '미등록')}"
            for item in self.external
        )
        return f"Riot Match-V5 실시간" + (f" · {external}" if external else " · 외부 검증 기록 미등록")

    def summaries(self) -> list[str]:
        return [
            f"**{item.source}**: {item.summary}"
            for item in self.external
            if item.status == "verified" and item.summary
        ]


def _load_records() -> dict[str, Any]:
    path = os.getenv("EXTERNAL_SCOUTING_JSON", "").strip()
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_evidence(riot_id: str) -> EvidenceBundle:
    record = _load_records().get(riot_id, {})
    if not isinstance(record, dict):
        record = {}
    items: list[SourceEvidence] = []
    for key, label in SOURCE_LABELS.items():
        raw = record.get(key, {})
        if not isinstance(raw, dict):
            raw = {}
        items.append(
            SourceEvidence(
                source=label,
                status="verified" if raw.get("verified") else "unregistered",
                summary=str(raw.get("summary", "")),
                url=str(raw.get("url", "")),
                verified_at=str(raw.get("verified_at", "")),
            )
        )
    return EvidenceBundle(external=tuple(items))


def merge_external_into_performance(performance: dict[str, Any], riot_id: str) -> dict[str, Any]:
    """Riot 요약에 외부 출처 상태를 추가하되 수치를 덮어쓰지 않는다."""
    bundle = load_evidence(riot_id)
    enriched = dict(performance)
    enriched["evidence_bundle"] = bundle
    return enriched
