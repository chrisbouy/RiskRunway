#!/usr/bin/env python3
"""
Benchmark quote normalization latency across configured LLM providers.

Pass 1 extraction is run once so provider timings focus on the LLM-backed
normalization pass. This keeps Groq vs Bedrock comparisons from being blurred by
PDF/OCR variability.
"""

import argparse
import statistics
import time
from pathlib import Path

import settings
from app.parsers.two_pass_parser import (
    pass1_extract_quote_layout,
    pass2_normalize_quote_data,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PDF = ROOT / "sample_docs" / "quote_rooster.PDF"


def benchmark_provider(provider, layout_data, runs):
    durations = []
    original_provider = settings.LLM_PROVIDER
    settings.LLM_PROVIDER = provider

    try:
        for run_number in range(1, runs + 1):
            start = time.time()
            pass2_normalize_quote_data(layout_data)
            duration = time.time() - start
            durations.append(duration)
            print(f"  {provider} run {run_number}: {duration:.2f}s")
    finally:
        settings.LLM_PROVIDER = original_provider

    return {
        "provider": provider,
        "runs": runs,
        "min": min(durations),
        "avg": statistics.mean(durations),
        "max": max(durations),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Compare Groq and Bedrock latency for quote Pass 2 normalization."
    )
    parser.add_argument(
        "pdf",
        nargs="?",
        default=str(DEFAULT_PDF),
        help=f"PDF to benchmark. Default: {DEFAULT_PDF}",
    )
    parser.add_argument(
        "--providers",
        nargs="+",
        default=["groq", "bedrock"],
        choices=["groq", "bedrock", "gemini"],
        help="Providers to compare.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Number of Pass 2 runs per provider.",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    if args.runs < 1:
        raise ValueError("--runs must be at least 1")

    print(f"Benchmarking {pdf_path}")
    print("Pass 1: extracting layout once...")
    pass1_start = time.time()
    layout_data = pass1_extract_quote_layout(str(pdf_path))
    pass1_duration = time.time() - pass1_start
    print(f"Pass 1 complete: {pass1_duration:.2f}s")

    results = []
    for provider in args.providers:
        print(f"\nPass 2 provider: {provider}")
        results.append(benchmark_provider(provider, layout_data, args.runs))

    print("\nResults")
    print("provider  runs  min     avg     max     estimated_total")
    for result in results:
        estimated_total = pass1_duration + result["avg"]
        print(
            f"{result['provider']:<8} "
            f"{result['runs']:<5} "
            f"{result['min']:<7.2f} "
            f"{result['avg']:<7.2f} "
            f"{result['max']:<7.2f} "
            f"{estimated_total:.2f}s"
        )

    by_provider = {result["provider"]: result for result in results}
    if "groq" in by_provider and "bedrock" in by_provider:
        groq_avg = by_provider["groq"]["avg"]
        bedrock_avg = by_provider["bedrock"]["avg"]
        if groq_avg > 0:
            speedup = bedrock_avg / groq_avg
            saved = bedrock_avg - groq_avg
            print(f"\nGroq Pass 2 speedup vs Bedrock: {speedup:.2f}x ({saved:.2f}s saved)")


if __name__ == "__main__":
    main()
