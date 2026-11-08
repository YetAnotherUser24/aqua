#!/usr/bin/env python3
"""
Generate publication-quality figure for Reviewer 1 (Obs 12):
Focuses on the core clean-water test bench nodes:
(a) Aerator Motor Node (nodo-aerador): 5 Hz actuation telemetry (Ts = 200 ms)
(b) Optical DO Sensor Node (nodo-sensor-od): 0.2 Hz luminescence probing (Ts = 5.0 s)
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
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                t = float(r.get("t_dev_ms") or 0)
                if t > 1600000000000:
                    t_devs.append(t)
            except:
                continue
    return np.array(t_devs)

aerator_t = load_data("/tmp/aerator_sample.csv")
sensor_t = load_data("/tmp/od_sensor_sample.csv")
mixer_t = load_data("/tmp/mixer_sample.csv")

# Aerator intervals in ms
aer_dts = np.diff(aerator_t)
aer_dts = aer_dts[(aer_dts >= 50) & (aer_dts <= 1000)]

# Sensor intervals in s
sen_dts = np.diff(sensor_t) / 1000.0
sen_dts = sen_dts[(sen_dts >= 1.0) & (sen_dts <= 15.0)]

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4.2), dpi=300)

# 1. Aerator Actuation Jitter Distribution (5 Hz, 200 ms)
ax1.hist(aer_dts, bins=60, color="#1e40af", alpha=0.75, edgecolor="black", linewidth=0.5, density=True)
ax1.axvline(200.0, color="#dc2626", linestyle="--", linewidth=1.5, label=r"Nominal $T_s = 200\,$ms (5 Hz)")
p95_aer = np.percentile(np.abs(aer_dts - 200.0), 95)
ax1.set_title(r"(a) Aerator Node: Actuation Interval ($\Delta t_{\mathrm{dev}}$)")
ax1.set_xlabel(r"Sampling Interval $\Delta t_{\mathrm{dev}}$ [ms]")
ax1.set_ylabel("Probability Density")
ax1.set_xlim(120, 320)
ax1.grid(True, linestyle=":", alpha=0.6)
ax1.legend(loc="upper right")
ax1.text(0.05, 0.72,
         f"Samples: {len(aer_dts):,}\nMean: {np.mean(aer_dts):.1f} ms\nStd Dev: {np.std(aer_dts):.1f} ms\n$P_{{95}}$ Error: $\pm${p95_aer:.1f} ms",
         transform=ax1.transAxes, bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="gray", alpha=0.9))

# 2. Optical DO Sensor Luminescence Poll Distribution (0.2 Hz, 5.0 s)
ax2.hist(sen_dts, bins=50, color="#047857", alpha=0.75, edgecolor="black", linewidth=0.5, density=True)
ax2.axvline(5.0, color="#dc2626", linestyle="--", linewidth=1.5, label=r"Nominal $T_s = 5.0\,$s (0.2 Hz)")
p95_sen = np.percentile(np.abs(sen_dts - 5.0), 95)
ax2.set_title(r"(b) Optical DO Sensor: Modbus Polling ($\Delta t_{\mathrm{dev}}$)")
ax2.set_xlabel(r"Sampling Interval $\Delta t_{\mathrm{dev}}$ [s]")
ax2.set_ylabel("Probability Density")
ax2.set_xlim(3.5, 8.5)
ax2.grid(True, linestyle=":", alpha=0.6)
ax2.legend(loc="upper right")
ax2.text(0.05, 0.72,
         f"Samples: {len(sen_dts):,}\nMedian: {np.median(sen_dts):.3f} s\nIQR: {np.percentile(sen_dts, 75) - np.percentile(sen_dts, 25):.3f} s\nMean: {np.mean(sen_dts):.3f} s",
         transform=ax2.transAxes, bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="gray", alpha=0.9))

plt.tight_layout()
out_png = "/root/aqua/docs/figure_sampling_jitter.png"
out_pdf = "/root/aqua/docs/figure_sampling_jitter.pdf"
plt.savefig(out_png)
plt.savefig(out_pdf)
print(f"Figure generated successfully with core paper nodes: {out_png} and {out_pdf}")
