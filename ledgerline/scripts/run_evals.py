"""Measure extraction accuracy, cost and latency against the generated ground truth.

This is how you answer "can the model do this reliably enough, cheaply enough?"
with numbers instead of impressions, and how you compare models or prompt changes.

    python -m scripts.run_evals                                   # uses LLM_PROVIDER / LLM_MODEL
    python -m scripts.run_evals --provider gemini --models gemini-3.5-flash-lite,gemini-3.8-flash
    python -m scripts.run_evals --limit 5 --sleep 4               # free-tier friendly

With --provider fake the score is 100% by construction: that run checks the harness, not a model.
"""

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import ValidationError

from app.extraction_schema import EXTRACTION_PROMPT, PROMPT_VERSION, ExtractedInvoice, json_schema
from app.llm import Attachment, LLMError

BASE = Path(__file__).resolve().parent.parent
HEADER_FIELDS = ["vendor_tax_id", "invoice_number", "invoice_date", "po_number", "subtotal", "tax_total", "total"]
MIME = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def make_provider(name: str, manifest: Path):
    if name == "fake":
        from app.llm.fake import FakeProvider

        return FakeProvider(manifest_path=manifest)
    if name == "gemini":
        import os

        from app.llm.gemini import GeminiProvider

        key = os.getenv("GEMINI_API_KEY")
        if not key:
            sys.exit("Set GEMINI_API_KEY to evaluate Gemini models.")
        return GeminiProvider(api_key=key)
    sys.exit(f"Unknown provider {name!r}")


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def same(field: str, expected, actual) -> bool:
    if field in {"subtotal", "tax_total", "total"}:
        return _dec(expected) == _dec(actual)
    if field == "invoice_date":
        return (str(expected) if expected else None) == (actual.isoformat() if actual else None)
    norm = lambda v: str(v).strip().upper() if v not in (None, "") else None  # noqa: E731
    return norm(expected) == norm(actual)


def lines_match(expected: list[dict], actual) -> bool:
    if len(expected) != len(actual):
        return False
    for e, a in zip(expected, actual, strict=True):
        if _dec(e["quantity"]) != _dec(a.quantity) or _dec(e["unit_price"]) != _dec(a.unit_price) or _dec(e["amount"]) != _dec(a.amount):
            return False
    return True


def call_with_retry(provider, attachment: Attachment, model: str, attempts: int = 4):
    for attempt in range(1, attempts + 1):
        try:
            return provider.generate_json(EXTRACTION_PROMPT, [attachment], json_schema(), model)
        except LLMError as exc:
            if not exc.retryable or attempt == attempts:
                raise
            wait = 5 * 2 ** (attempt - 1)
            print(f"    retryable error, waiting {wait}s: {str(exc)[:120]}", file=sys.stderr)
            time.sleep(wait)


def evaluate(provider, model: str, items: list[dict], manifest_dir: Path, sleep: float) -> dict:
    results = []
    for n, item in enumerate(items, 1):
        path = manifest_dir / item["file"]
        attachment = Attachment(data=path.read_bytes(), mime_type=MIME[path.suffix.lower()], sha256=item["sha256"])
        row = {"file": item["file"], "scenario": item["scenario"], "fields": {}, "error": None}
        try:
            result = call_with_retry(provider, attachment, model)
            row.update(cost_usd=float(result.cost_usd) if result.cost_usd is not None else None,
                       latency_ms=result.latency_ms, input_tokens=result.input_tokens, output_tokens=result.output_tokens)
            extracted = ExtractedInvoice.model_validate(json.loads(result.text))
            truth = item["truth"]
            for field in HEADER_FIELDS:
                row["fields"][field] = same(field, truth[field], getattr(extracted, field))
            row["fields"]["line_items"] = lines_match(truth["line_items"], extracted.line_items)
        except (LLMError, json.JSONDecodeError, ValidationError) as exc:
            row["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        row["all_correct"] = bool(row["fields"]) and all(row["fields"].values())
        mark = "ok " if row["all_correct"] else ("ERR" if row["error"] else "x  ")
        wrong = [f for f, okay in row["fields"].items() if not okay]
        print(f"  [{n:>2}/{len(items)}] {mark} {item['file'].split('/')[-1]}" + (f"  wrong: {', '.join(wrong)}" if wrong else "")
              + (f"  {row['error'][:80]}" if row["error"] else ""))
        results.append(row)
        if sleep and n < len(items):
            time.sleep(sleep)
    return summarise(model, results)


def summarise(model: str, results: list[dict]) -> dict:
    valid = [r for r in results if not r["error"]]
    fields = HEADER_FIELDS + ["line_items"]
    field_acc = {f: (sum(r["fields"][f] for r in valid) / len(results)) for f in fields} if results else {}
    by_scenario: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        by_scenario[r["scenario"]].append(r["all_correct"])
    costs = [r["cost_usd"] for r in valid if r.get("cost_usd") is not None]
    latencies = sorted(r["latency_ms"] for r in valid if r.get("latency_ms") is not None)
    return {
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "n": len(results),
        "schema_valid_rate": len(valid) / len(results) if results else 0,
        "all_fields_correct_rate": sum(r["all_correct"] for r in results) / len(results) if results else 0,
        "field_accuracy": field_acc,
        "scenario_accuracy": {s: sum(v) / len(v) for s, v in sorted(by_scenario.items())},
        "total_cost_usd": sum(costs) if costs else None,
        "cost_per_invoice_usd": (sum(costs) / len(costs)) if costs else None,
        "latency_p50_ms": statistics.median(latencies) if latencies else None,
        "latency_p95_ms": latencies[max(0, int(len(latencies) * 0.95) - 1)] if latencies else None,
        "results": results,
    }


def print_report(summaries: list[dict]) -> str:
    pct = lambda x: f"{x * 100:.0f}%"  # noqa: E731
    money = lambda x: "n/a" if x is None else f"${x:.5f}"  # noqa: E731
    lines = ["", "| Metric | " + " | ".join(s["model"] for s in summaries) + " |",
             "|---|" + "---|" * len(summaries)]
    rows = [("Invoices", lambda s: str(s["n"])), ("Valid output", lambda s: pct(s["schema_valid_rate"])),
            ("All fields correct", lambda s: pct(s["all_fields_correct_rate"]))]
    rows += [(f"  {f}", lambda s, f=f: pct(s["field_accuracy"].get(f, 0))) for f in HEADER_FIELDS + ["line_items"]]
    scenarios = sorted({k for s in summaries for k in s["scenario_accuracy"]})
    rows += [(f"Scenario: {sc}", lambda s, sc=sc: pct(s["scenario_accuracy"].get(sc, 0))) for sc in scenarios]
    rows += [("Cost per invoice", lambda s: money(s["cost_per_invoice_usd"])), ("Total cost", lambda s: money(s["total_cost_usd"])),
             ("Latency p50", lambda s: f"{s['latency_p50_ms']} ms"), ("Latency p95", lambda s: f"{s['latency_p95_ms']} ms")]
    for label, get in rows:
        lines.append(f"| {label} | " + " | ".join(get(s) for s in summaries) + " |")
    report = "\n".join(lines)
    print(report)
    return report


def main() -> None:
    import os

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=BASE / "data" / "generated" / "manifest.json")
    parser.add_argument("--provider", default=os.getenv("LLM_PROVIDER", "fake"))
    parser.add_argument("--models", default=os.getenv("LLM_MODEL", "gemini-3.5-flash-lite"))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--scenario", default=None, help="Only evaluate one scenario, e.g. scanned")
    parser.add_argument("--sleep", type=float, default=0, help="Seconds between calls (stay under rate limits)")
    args = parser.parse_args()

    if not args.manifest.exists():
        sys.exit("No manifest found. Run: python -m scripts.generate_invoices")
    manifest = json.loads(args.manifest.read_text())
    items = [i for i in manifest["invoices"] if not args.scenario or i["scenario"] == args.scenario]
    if args.limit:
        items = items[: args.limit]

    provider = make_provider(args.provider, args.manifest)
    summaries = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        print(f"\nEvaluating {args.provider}/{model} on {len(items)} invoices (prompt {PROMPT_VERSION})")
        summaries.append(evaluate(provider, model, items, args.manifest.parent, args.sleep))

    report = print_report(summaries)
    out_dir = BASE / "data" / "eval-results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    (out_dir / f"{stamp}.json").write_text(json.dumps(summaries, indent=2, default=str))
    (out_dir / f"{stamp}.md").write_text(f"# Eval run {stamp} ({args.provider})\n{report}\n")
    print(f"\nSaved results to {out_dir / stamp}.json and .md")


if __name__ == "__main__":
    main()
