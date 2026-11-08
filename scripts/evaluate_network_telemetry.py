#!/usr/bin/env python3
"""
AquaControl — Empirical Network Reliability & Temporal Jitter Evaluator
========================================================================
Analyzes raw telemetry or export bundles to quantify:
- Packet Delivery Ratio (PDR) from monotonic sequence numbers (seq)
- Packet / Envelope loss rate (%)
- Sampling period (dt) and sampling jitter (std dev, P95, P99)
- Generates ASCII and LaTeX tables ready for scientific publication.

Usage:
    python3 evaluate_network_telemetry.py [path_or_glob]
    Default: /data/telemetry/*/*.csv
"""

import argparse
import csv
import glob
import os
import sys
from collections import defaultdict
import numpy as np


def analyze_file(path: str):
    role = os.path.basename(os.path.dirname(path))
    if role in ("events", "unknown"):
        return None

    t_devs = []
    seqs = []

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                t = float(r.get("t_dev_ms") or 0)
                s = int(r.get("seq") or 0)
                if t > 1600000000000:  # Valid epoch ms (>= year 2020)
                    t_devs.append(t)
                    seqs.append(s)
            except (ValueError, TypeError):
                continue

    if not t_devs:
        return None

    t_devs = np.array(t_devs)
    unique_seqs = sorted(list(set(seqs)))

    if len(unique_seqs) > 1:
        expected_envelopes = unique_seqs[-1] - unique_seqs[0] + 1
        received_envelopes = len(unique_seqs)
        lost_envelopes = max(0, expected_envelopes - received_envelopes)
        pdr = (received_envelopes / expected_envelopes) * 100.0 if expected_envelopes > 0 else 100.0
        loss_pct = (lost_envelopes / expected_envelopes) * 100.0 if expected_envelopes > 0 else 0.0
    else:
        expected_envelopes = len(unique_seqs)
        received_envelopes = len(unique_seqs)
        lost_envelopes = 0
        pdr = 100.0
        loss_pct = 0.0

    dts = np.diff(t_devs) / 1000.0  # seconds
    # Filter active experiment intervals between 0.05s and 15s (discards pauses between runs)
    dts_active = dts[(dts >= 0.05) & (dts <= 15.0)]

    if len(dts_active) > 0:
        mean_dt = np.mean(dts_active)
        std_jitter_ms = np.std(dts_active) * 1000.0
        p95_jitter_ms = np.percentile(np.abs(dts_active - mean_dt), 95) * 1000.0
        p99_jitter_ms = np.percentile(np.abs(dts_active - mean_dt), 99) * 1000.0
        cadence_hz = 1.0 / mean_dt
    else:
        mean_dt = 0.0
        std_jitter_ms = 0.0
        p95_jitter_ms = 0.0
        p99_jitter_ms = 0.0
        cadence_hz = 0.0

    return {
        "role": role,
        "filename": os.path.basename(path),
        "total_samples": len(t_devs),
        "recv_envelopes": received_envelopes,
        "exp_envelopes": expected_envelopes,
        "lost_envelopes": lost_envelopes,
        "pdr": pdr,
        "loss_pct": loss_pct,
        "mean_dt": mean_dt,
        "cadence_hz": cadence_hz,
        "std_jitter_ms": std_jitter_ms,
        "p95_jitter_ms": p95_jitter_ms,
        "p99_jitter_ms": p99_jitter_ms,
    }


def main():
    parser = argparse.ArgumentParser(description="Evaluate AquaControl Network & Temporal Metrics")
    parser.add_argument(
        "target",
        nargs="?",
        default="/data/telemetry/*/*.csv",
        help="Path or glob pattern for telemetry files (default: /data/telemetry/*/*.csv)",
    )
    parser.add_argument("--latex", action="store_true", help="Print LaTeX table format for publication")
    args = parser.parse_args()

    files = sorted(glob.glob(args.target))
    if not files and os.path.isfile(args.target):
        files = [args.target]

    if not files:
        print(f"No telemetry files found matching: {args.target}", file=sys.stderr)
        sys.exit(1)

    results = []
    for f in files:
        res = analyze_file(f)
        if res:
            results.append(res)

    if not results:
        print("No valid telemetry records found.", file=sys.stderr)
        sys.exit(1)

    # ASCII Table
    hdr = f"{'Node / Role':<14} | {'Dataset File':<16} | {'Samples':<8} | {'Envelopes':<11} | {'PDR (%)':<8} | {'Loss (%)':<9} | {'Cadence':<10} | {'Jitter (ms)':<12} | {'P95 (ms)':<9}"
    print("\n" + "=" * len(hdr))
    print(" AQUACONTROL EXPERIMENTAL NETWORK & TIMING EVALUATION")
    print("=" * len(hdr))
    print(hdr)
    print("-" * len(hdr))

    for r in results:
        cadence_str = f"{r['cadence_hz']:.2f} Hz" if r["cadence_hz"] > 0 else "N/A"
        print(
            f"{r['role']:<14} | {r['filename']:<16} | {r['total_samples']:<8} | {r['recv_envelopes']:<11} | {r['pdr']:<8.2f} | {r['loss_pct']:<9.2f} | {cadence_str:<10} | {r['std_jitter_ms']:<12.2f} | {r['p95_jitter_ms']:<9.2f}"
        )
    print("=" * len(hdr) + "\n")

    # LaTeX Table output if requested or summary
    if args.latex:
        print("% --- LaTeX Table Snippet for IEEE / Elsevier Manuscript ---")
        print(r"\begin{table}[htbp]")
        print(r"\centering")
        print(r"\caption{Empirical Network Reliability and Temporal Sampling Metrics across Nodes}")
        print(r"\label{tab:network_metrics}")
        print(r"\begin{tabular}{lcccccc}")
        print(r"\hline")
        print(r"\textbf{Subsystem / Node} & \textbf{Nominal Rate} & \textbf{Samples} & \textbf{Envelopes} & \textbf{PDR (\%)} & \textbf{Jitter $\sigma_{\Delta t}$ (ms)} & \textbf{$P_{95}$ Error (ms)} \\")
        print(r"\hline")
        for r in results:
            role_esc = r["role"].replace("_", r"\_")
            cadence_str = f"{r['cadence_hz']:.2f} Hz" if r["cadence_hz"] > 0 else "N/A"
            print(
                f"{role_esc} & {cadence_str} & {r['total_samples']} & {r['recv_envelopes']} & {r['pdr']:.2f}\\% & {r['std_jitter_ms']:.2f} & {r['p95_jitter_ms']:.2f} \\\\"
            )
        print(r"\hline")
        print(r"\end{tabular}")
        print(r"\end{table}")


if __name__ == "__main__":
    main()
