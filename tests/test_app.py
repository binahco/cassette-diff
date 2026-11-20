from __future__ import annotations

import json
from pathlib import Path

from llm_client.provider import ProviderRequest, ProviderResponse
from test_kit import EvalDataset, run as run_evalset

from cassette_diff.main import (
    DEFAULT_DATASET,
    UMBRAL_MENOR,
    build_client,
    compare_cassettes,
    judge_for,
    verdict_por_similitud,
)

STUB_TEXT = json.dumps(
    {"summary": "deriva menor de redacción", "verdict": "drift_menor", "changed_cases": ["caso 0"]},
    ensure_ascii=False,
)


class StubProvider:
    name = "stub"

    def __init__(self, text: str | None = None) -> None:
        self.text = text or STUB_TEXT
        self.calls = 0
        self.last_request: ProviderRequest | None = None

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        self.last_request = request
        return ProviderResponse(text=self.text, model=request.model)

    def stream(self, request: ProviderRequest) -> list[str]:
        self.calls += 1
        return [self.text]


def write_tape(tmp_path: Path, name: str, entries: list[dict]) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n")
    return path


def test_iguales_por_indice_con_volatiles_ignorados(tmp_path) -> None:
    a = write_tape(tmp_path, "a.jsonl", [
        {"model": "m", "text": "respuesta uno", "timestamp": "1", "span_id": "x"},
        {"model": "m", "text": "respuesta dos", "timestamp": "2"},
    ])
    b = write_tape(tmp_path, "b.jsonl", [
        {"model": "m", "text": "respuesta uno", "timestamp": "9", "span_id": "y"},
        {"model": "m", "text": "respuesta dos", "timestamp": "9"},
    ])
    out = compare_cassettes(a, b)
    assert out["comparados"] == 2
    assert all(caso["veredicto"] == "sin_cambio" for caso in out["casos"])
    assert out["casos"][0]["volatiles_ignorados"]


def test_deriva_de_texto_se_ve_en_hunks_y_veredicto(tmp_path) -> None:
    a = write_tape(tmp_path, "a.jsonl", [{"model": "m", "text": "uno\ndos\ntres", "timestamp": "1"}])
    b = write_tape(tmp_path, "b.jsonl", [{"model": "m", "text": "uno\ndos\nTRES", "timestamp": "2"}])
    out = compare_cassettes(a, b)
    caso = out["casos"][0]
    assert caso["hunks"] == 1
    assert caso["veredicto"] == "drift_menor"
    assert caso["campos_cambiados"] == ["text"]


def test_umbrales_explicitos_clasifican() -> None:
    assert verdict_por_similitud(1.0, 0) == "sin_cambio"
    assert verdict_por_similitud(0.97, 1) == "drift_menor"
    assert verdict_por_similitud(0.40, 3) == "drift_mayor"
    assert UMBRAL_MENOR > 0.9  # el umbral menor es estricto: casi igual no es igual


def test_cassette_de_longitudes_distintas_no_miente(tmp_path) -> None:
    a = write_tape(tmp_path, "a.jsonl", [{"text": "x"}, {"text": "y"}])
    b = write_tape(tmp_path, "b.jsonl", [{"text": "x"}])
    out = compare_cassettes(a, b)
    assert out["comparados"] == 1 and out["solo_en_a"] == 1


def test_nada_sensible_entra_al_prompt_del_narrator(tmp_path) -> None:
    a = write_tape(tmp_path, "a.jsonl", [{"text": "token ghp_AbCdEfGhIjKlMnOpQrStUvWxYz123456 aquí"}])
    b = write_tape(tmp_path, "b.jsonl", [{"text": "otro"}])
    from cassette_diff.main import narrate
    from datetime import date

    stub = StubProvider()
    narrate(build_client(stub)["client"], compare_cassettes(a, b), date(2026, 11, 20))
    mensaje = json.dumps([m["content"] for m in stub.last_request.messages], ensure_ascii=False)
    assert "ghp_" not in mensaje, "el secreto del cassette entró al prompt"


def test_el_judge_evoluciona_el_dataset_completo(tmp_path) -> None:
    dataset = EvalDataset.from_jsonl(DEFAULT_DATASET)
    client = build_client(StubProvider())["client"]
    out = run_evalset(dataset, judge_for(client), mode="full", threshold=1.0)
    assert out.total == 3
    assert out.passed == 3
