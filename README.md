# cassette-diff

Deriva del modelo (semana 19): dos grabaciones del **mismo prompt con la misma entrada**
se comparan campo a campo — texto, uso de tokens, estructura — y la diferencia se narra
con un LLM validado. Los modelos cambian sin avisar; el cassette que pasó verde el lunes
puede contar otra historia el viernes y nadie mira. Este repo hace que mirar sea barato.

Nace con el módulo nuevo del core: **`diff-engine` (C12, ADR-10)** — comparación
estructural determinista que **ignora los volátiles declarados y los lista** (un
`timestamp` distinto no es deriva; esconder que se ignoró, sí sería mentir).

## Demo

```bash
uv sync
uv run cassette-diff demo-record   # graba el mismo caso dos veces (2 llamadas reales)
uv run cassette-diff demo          # compara las dos sesiones y narra
# {
#   "comparados": 1,
#   "casos": [{"caso": 0, "similitud_texto": 0.93, "hunks": 1, …,
#              "veredicto": "drift_menor"}]
# }
```

También contra cassettes cualquiera:

```bash
uv run cassette-diff compare cassettes/sesion-a/complete-*.jsonl cassettes/sesion-b/complete-*.jsonl
```

La comparación es **determinista** (difflib, D4): mismos archivos, mismo reporte. La
narración es LLM sobre el resumen ya saneado (`secure-base`) y valida contra
`drift-report-v1` o falla. El veredicto por umbral es explícito (`0.95 / 0.60`): lo
semántico no se mide aquí — eso es `vector-core` (C11, con deuda abierta).

## Cómo funciona

```
cassette-diff compare a.jsonl b.jsonl
  ├─ diff_engine.diff_records(caso_i(a), caso_i(b))   → campos, hunks, volátiles
  ├─ veredicto_por_similitud(similitud, hunks)        → sin_cambio|drift_menor|drift_mayor
  └─ (opcional) narrate: LLM validado (drift-report-v1) sobre el resumen redactado
```

- Los casos se alinean **por índice**; lo que sobra en una cinta se cuenta
  (`solo_en_a/solo_en_b`), no se descarta en silencio.
- La demo usa su propio caso: `demo-record` graba el MISMO input en dos sesiones y
  `demo` mide la deriva real del modelo entre las dos llamadas.

## Recicla de

| Módulo | Uso |
|---|---|
| `diff-engine` | seed (C12): la comparación campo a campo con hunks y volátiles declarados |
| `llm-client` | la llamada del narrator (retry + reparación + span de 20 campos) |
| `schema-validate` | la narración valida contra `drift-report-v1` o falla |
| `secure-base` | el resumen que entra al prompt va saneado: un secreto en un cassette no entra al modelo |
| `cache-ratelimit` | caché TTL + token bucket alrededor del proveedor |
| `test-kit` | el prompt nace evaluado: dataset congelado + replay (D4) |

## Limitaciones

- Alineación por índice: si una cinta se reordena, comparar es mentir — se detecta
  porque todo "cambia"; el matching por clave queda para el segundo consumidor.
- La similitud es **textual**, no semántica: dos paráfrasis son deriva para este
  instrumento. Vector-core (C11) traerá lo otro.
- Los umbrales son del proyecto, no una verdad: están escritos y se pueden discutir.

## Roadmap

- [x] Comparación determinista + narración validada + demo con deriva real (sem. 19)
- [x] Seed de `diff-engine` (C12) con volátiles declarados
- [ ] Alineación de casos por clave (prompt_id+input) en vez de índice
- [ ] Vigilancia programada: deriva semanal del corpus de la flota (con `bot-base`)
