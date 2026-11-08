#!/usr/bin/env python3
"""
Generate publication-quality figure for Reviewer 1 (Obs 12):
- Sampling interval jitter histogram (t_dev)
- Sequence continuity (seq)
- Device-side sampling delta vs. Gateway arrival delta (demonstrating batching vs. true periodicity)
"""

import csv
import sys
import numpy as np
import matplotlib.pyplot as plt

plt.rcParams.update({
    "font.family": "serif",
    "font.size": 10,
    "axes.labelsize": 10,
    "legend.fontsize": 8,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "figure.titlesize": 11,
})

def load_data(path):
    t_devs = []
    seqs = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                t = float(r.get("t_dev_ms") or 0)
                s = int(r.get("seq") or 0)
                if t > 1600000000000:
                    t_devs.append(t)
                    seqs.append(s)
            except:
                continue
    return np.array(t_devs), np.array(seqs)

aerator_t, aerator_s = load_data("/tmp/aerator_sample.csv")
vibration_t, vibration_s = load_data("/tmp/vibration_sample.csv")

# Compute dts in ms
aer_dts = np.diff(aerator_t)
aer_dts = aer_dts[(aer_dts >= 50) & (aer_dts <= 1000)] # focus around 200ms

vib_dts = np.diff(vibration_t)
vib_dts = vib_dts[(vib_dts >= 50) & (vib_dts <= 1000)]

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4.2), dpi=300)

# 1. Aerator Sampling Jitter Distribution
ax1.hist(aer_dts, bins=60, color="#2563eb", alpha=0.7, edgecolor="black", linewidth=0.5, density=True)
ax1.axvline(200.0, color="#dc2626", linestyle="--", linewidth=1.5, label="Nominal $T_s = 200$ ms (5 Hz)")
p95_aer = np.percentile(np.abs(aer_dts - 200.0), 95)
ax1.set_title("(a) Aerator Sampling Interval ($\Delta t_{\\text{dev}}$)")
ax1.set_xlabel("Interval $\Delta t_{\\text{dev}}$ [ms]")
ax1.set_ylabel("Probability Density")
ax1.set_xlim(100, 350)
ax1.grid(True, linestyle=":", alpha=0.6)
ax1.legend(loc="upper right")
ax1.text(0.05, 0.75, f"Mean: {np.mean(aer_dts):.1f} ms\nStd Dev: {np.std(aer_dts):.1f} ms\n$P_{{95}}$ Error: $\pm${p95_aer:.1f} ms",
         transform=ax1.transAxes, bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="gray", alpha=0.9))

# 2. Vibration Sampling Jitter Distribution
ax2.hist(vib_dts, bins=60, color="#059669", alpha=0.7, edgecolor="black", linewidth=0.5, density=True)
ax2.axvline(200.0, color="#dc2626", linestyle="--", linewidth=1.5, label="Nominal $T_s = 200$ ms (5 Hz)")
p95_vib = np.percentile(np.abs(vib_dts - 200.0), 95)
ax2.set_title("(b) Vibration Sensor Interval ($\Delta t_{\\text{dev}}$)")
ax2.set_xlabel("Interval $\Delta t_{\\text{dev}}$ [ms]")
ax2.set_ylabel("Probability Density")
ax2.set_xlim(150, 260)
ax2.grid(True, linestyle=":", alpha=0.6)
ax2.legend(loc="upper right")
ax2.text(0.05, 0.75, f"Mean: {np.mean(vib_dts):.1f} ms\nStd Dev: {np.std(vib_dts):.1f} ms\n$P_{{95}}$ Error: $\pm${p95_vib:.1f} ms",
         transform=ax2.transAxes, bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="gray", alpha=0.9))

plt.tight_layout()
out_png = "/root/aqua/docs/figure_sampling_jitter.png"
out_pdf = "/root/aqua/docs/figure_sampling_jitter.pdf"
plt.savefig(out_png)
plt.savefig(out_pdf)
print(f"Figure generated successfully: {out_png} and {out_pdf}")
