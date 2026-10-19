"""Barrido de parámetros: genera variantes de candidate_strategy.py y las simula en paralelo.

Uso (desde la raíz del repositorio):
    python submission/generated_artifacts/tune.py [5k|150k] 'HOLD_MINUTES=0;SCALE=1' ...
Cada argumento es una variante: asignaciones ``NOMBRE=valor`` separadas por ``;``.
SCALE equivale a VALUE_SCALE. El resto sustituye la constante homónima.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASETS = {"5k": ROOT / "data", "150k": ROOT / "scenarios" / "public_150k"}


def build_variant(source: str, assignments: dict[str, str]) -> str:
    if "SCALE" in assignments:
        assignments["VALUE_SCALE"] = assignments.pop("SCALE")
    for name, value in assignments.items():
        source, n = re.subn(rf"^{name} = .*$", f"{name} = {value}", source, flags=re.M)
        if n != 1:
            raise SystemExit(f"constante no encontrada: {name}")
    return source


def run_variant(label: str, dataset: str, workdir: Path) -> dict:
    assignments = dict(item.split("=", 1) for item in label.split(";") if item)
    base = (ROOT / "candidate_strategy.py").read_text(encoding="utf-8")
    variant_dir = workdir / re.sub(r"\W+", "_", label or "base")
    variant_dir.mkdir(parents=True, exist_ok=True)
    strategy = variant_dir / "candidate_strategy.py"
    strategy.write_text(build_variant(base, dict(assignments)), encoding="utf-8")
    output = variant_dir / f"report_{dataset}.json"
    proc = subprocess.run(
        [
            sys.executable, "-m", "log8092_engine.cli",
            "--data", str(DATASETS[dataset]),
            "--strategy", str(strategy),
            "--interfaces", str(ROOT / "interfaces.py"),
            "--output", str(output),
        ],
        cwd=ROOT / "engine", capture_output=True, text=True,
    )
    if not output.exists():
        return {"label": label, "error": proc.stderr[-300:]}
    r = json.loads(output.read_text(encoding="utf-8"))
    return {
        "label": label,
        "status": r["status"],
        "cost": r["costs"]["total_cents"],
        "mean": r["lead_time_minutes"]["mean"],
        "p95": r["lead_time_minutes"]["p95"],
        "rcp": r["rcp"]["weighted_score"],
        "sla": r["sla"]["breach_rate"],
        "unres": r["counts"]["unresolved"],
        "holds": r["counts"]["voluntary_hold_breaches"],
        "cpu": r["resources"]["cpu_seconds"],
        "rss_mb": round(r["resources"]["peak_rss_bytes"] / 1048576),
    }


def main() -> None:
    dataset = sys.argv[1]
    labels = sys.argv[2:] or [""]
    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(max_workers=4) as pool:
        for row in pool.map(lambda lb: run_variant(lb, dataset, Path(tmp)), labels):
            print(row, flush=True)


if __name__ == "__main__":
    main()
