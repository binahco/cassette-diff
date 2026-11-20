from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

import yaml
from cache_ratelimit import CachedProvider, RateLimiter, RealClock, ThrottledProvider
from diff_engine import diff_records
from llm_client import CompletionRequest, CompletionResult, LlmClient, ReplayProvider, Span
from llm_client.providers.opencode_cli import OpenCodeCLI
from schema_validate import SchemaRegistry
from secure_base import sanitize_for_prompt
from test_kit import EvalCase, EvalDataset, run

from .models import DriftReport

DEFAULT_MODEL = "opencode/big-pickle"
SCHEMA_ID = "drift-report-v1"
PROMPT_ID = "drift-narrator"
ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = ROOT / "prompts"
DEFAULT_PROMPT = PROMPT_DIR / "drift-narrator.md"
DEFAULT_DATASET = ROOT / "evals" / "drift-narrator.jsonl"
DEMO_CASE = 0
UMBRAL_MENOR = 0.95
UMBRAL_MAYOR = 0.60


def load_prompt(path: Path) -> tuple[str, str, str, Path]:
    text = path.read_text()
    if not text.startswith("---"):
        raise SystemExit(f"{path}: falta frontmatter")
    _, frontmatter, body = text.split("---", 2)
    data = yaml.safe_load(frontmatter)
    eval_path = ROOT / data["eval"] if not Path(data["eval"]).is_absolute() else Path(data["eval"])
    return data["id"], data["version"], body.strip(), eval_path


def render_drift(prompt_id: str, prompt_version: str, variables: dict) -> list[dict]:
    """Prompt templado para `drift-narrator`; cualquier otro prompt pasa crudo."""
    if prompt_id == PROMPT_ID:
        _, _, body, _ = load_prompt(DEFAULT_PROMPT)
        system_part = body.split("## Sistema\n", 1)[1].split("## Usuario\n", 1)[0].strip()
        user_part = body.split("## Usuario\n", 1)[1].strip().format(**variables)
        return [
            {"role": "system", "content": system_part},
            {"role": "user", "content": user_part},
        ]
    payload = {"prompt_id": prompt_id, "prompt_version": prompt_version, "variables": variables}
    return [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def build_emitter(span_file: Path | None):
    def emit(span: Span, _result) -> None:
        if span_file is not None:
            with span_file.open("a") as handle:
                handle.write(span.as_jsonl() + "\n")
        else:
            sys.stderr.write(span.as_jsonl() + "\n")

    return emit


def build_client(inner_provider, *, clock=None, span_file: Path | None = None) -> dict:
    clock = RealClock() if clock is None else clock
    cached = CachedProvider(inner_provider, ttl_seconds=3600.0, clock=clock)
    limiter = RateLimiter(rpm=120, burst=20, clock=clock)
    provider = ThrottledProvider(cached, limiter)
    registry = SchemaRegistry()
    registry.register(SCHEMA_ID, DriftReport)
    client = LlmClient(
        provider,
        consumer_repo="cassette-diff",
        model_aliases={"fast": DEFAULT_MODEL},
        renderer=render_drift,
        validator=registry.make_validator(SCHEMA_ID),
        emitter=build_emitter(span_file),
    )
    return {"client": client, "cached": cached, "limiter": limiter}


# --------------------------------------------------------------------------
# comparación determinista: dos jsonl de cassette, caso a caso por índice
# --------------------------------------------------------------------------
def load_cassette(path: Path) -> list[dict]:
    entries = []
    for line in path.read_text().splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def _flatten(entry: dict) -> dict:
    """Aplana una entrada de cassette de la flota ({model, request, response{text, usage}})
    a lo que diff-engine compara bien: texto plano y contables. Las entradas ya planas
    pasan tal cual."""
    if not isinstance(entry.get("response"), dict):
        return entry
    response = entry["response"]
    flat = {"model": entry.get("model"), "text": response.get("text", "")}
    usage = response.get("usage") or {}
    flat["usage"] = json.dumps(usage, sort_keys=True)
    return flat


def verdict_por_similitud(similitud: float, hunks: int) -> str:
    """Umbral explícito (ADR-10): lo semántico no se mide aquí (C11 pendiente)."""
    if similitud >= UMBRAL_MENOR and hunks == 0:
        return "sin_cambio"
    if similitud >= UMBRAL_MAYOR:
        return "drift_menor"
    return "drift_mayor"


def compare_cassettes(path_a: Path, path_b: Path) -> dict:
    a, b = load_cassette(path_a), load_cassette(path_b)
    n = min(len(a), len(b))
    casos = []
    for i in range(n):
        report = diff_records(_flatten(a[i]), _flatten(b[i]))
        casos.append(
            {
                "caso": i,
                "igual": report.igual,
                "similitud_texto": report.similitud_texto,
                "hunks": len(report.hunks),
                "hunks_detalle": [h.corto for h in report.hunks[:4]],
                "campos_cambiados": [c.campo for c in report.campos if not c.igual],
                "volatiles_ignorados": report.volatiles_ignorados,
                "veredicto": verdict_por_similitud(report.similitud_texto, len(report.hunks)),
            }
        )
    return {"comparados": n, "solo_en_a": len(a) - n, "solo_en_b": len(b) - n, "casos": casos}


def comparison_text(comparison: dict) -> str:
    lineas = []
    for caso in comparison["casos"]:
        lineas.append(
            f"caso {caso['caso']}: similitud_texto={caso['similitud_texto']}, "
            f"hunks={caso['hunks']}, campos_cambiados={caso['campos_cambiados'] or 'ninguno'} · "
            f"volátiles ignorados: {', '.join(caso['volatiles_ignorados']) or 'ninguno'}"
        )
    return "\n".join(lineas) if lineas else "(nada comparable)"


def narrate(client: LlmClient, comparison: dict, today: date) -> DriftReport:
    result = client.complete(
        CompletionRequest(
            prompt_id=PROMPT_ID,
            prompt_version="0.1.0",
            variables={
                "date": today.isoformat(),
                "comparison": sanitize_for_prompt(comparison_text(comparison)).text.replace("{", "(").replace("}", ")"),
            },
            model_alias="fast",
            response_schema=SCHEMA_ID,
            tags=["cassette-diff", "week-19"],
        )
    )
    if not result.validation.ok:
        raise RuntimeError("; ".join(result.validation.errors))
    return result.parsed


def judge_for(client: LlmClient) -> Callable[[EvalCase], CompletionResult]:
    def judge(case: EvalCase) -> CompletionResult:
        return client.complete(
            CompletionRequest(
                prompt_id=case.prompt_id,
                prompt_version=case.prompt_version,
                variables=case.input,
                model_alias="fast",
                response_schema=SCHEMA_ID,
                tags=["cassette-diff", "week-19"],
            )
        )

    return judge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cassette-diff", description="Deriva del modelo entre dos grabaciones del mismo prompt (semana 19).")
    parser.add_argument("command", nargs="?", default="help", choices=["compare", "demo-record", "demo", "record", "replay-check"])
    parser.add_argument("paths", nargs="*", help="compare: cassette_a.jsonl cassette_b.jsonl")
    parser.add_argument("--narrate", action="store_true", help="compare: además narra con LLM validado")
    args = parser.parse_args(argv)

    if args.command == "record":
        dataset = EvalDataset.from_jsonl(DEFAULT_DATASET)
        provider = ReplayProvider(ROOT / "cassettes", record=True, inner=OpenCodeCLI(DEFAULT_MODEL))
        report = run(dataset, judge_for(build_client(provider)["client"]), mode="full", threshold=1.0)
        print(f"grabadas {len(dataset.cases)} respuestas; pass={report.passed}/{report.total}")
        return 0 if report.threshold_ok else 1

    if args.command == "replay-check":
        dataset = EvalDataset.from_jsonl(DEFAULT_DATASET)
        provider = ReplayProvider(ROOT / "cassettes", record=False)
        report = run(dataset, judge_for(build_client(provider)["client"]), mode="full", threshold=1.0)
        print(f"replay: pass={report.passed}/{report.total} threshold_ok={report.threshold_ok}")
        return 0 if report.threshold_ok else 1

    if args.command == "compare":
        if len(args.paths) != 2:
            parser.error("compare exige dos archivos .jsonl")
        comparison = compare_cassettes(Path(args.paths[0]), Path(args.paths[1]))
        salida = dict(comparison)
        if args.narrate:
            reporte = narrate(build_client(OpenCodeCLI(DEFAULT_MODEL))["client"], comparison, date.today())
            salida["narrativa"] = reporte.model_dump()
        print(json.dumps(salida, ensure_ascii=False, indent=2))
        return 0

    if args.command in {"demo-record", "demo"}:
        # Deriva real del modelo: el MISMO caso del dataset propio, grabado dos veces.
        # Las sesiones viven en cassettes/sesion-a/ y cassettes/sesion-b/; el narrator
        # tiene su propia cinta (la de record). demo-record graba, demo compara.
        dataset = EvalDataset.from_jsonl(DEFAULT_DATASET)
        caso = dataset.cases[DEMO_CASE]
        for sesion in ("sesion-a", "sesion-b"):
            if args.command == "demo-record":
                provider = ReplayProvider(ROOT / "cassettes" / sesion, record=True, inner=OpenCodeCLI(DEFAULT_MODEL))
                client = build_client(provider)["client"]
                client.complete(
                    CompletionRequest(
                        prompt_id=PROMPT_ID, prompt_version="0.1.0", variables=dict(caso.input),
                        model_alias="fast", response_schema=SCHEMA_ID, tags=["cassette-diff", "demo", sesion],
                    )
                )
        dir_a = ROOT / "cassettes" / "sesion-a"
        dir_b = ROOT / "cassettes" / "sesion-b"
        if args.command == "demo-record":
            print(f"grabadas ambas sesiones en {dir_a} y {dir_b}")
            return 0
        tape_a = next(dir_a.glob("complete-*.jsonl"))
        tape_b = next(dir_b.glob("complete-*.jsonl"))
        comparison = compare_cassettes(tape_a, tape_b)
        print(json.dumps(comparison, ensure_ascii=False, indent=2))
        provider = ReplayProvider(ROOT / "cassettes", record=False)
        reporte = narrate(build_client(provider)["client"], comparison, date.today())
        print(json.dumps(reporte.model_dump(), ensure_ascii=False))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
