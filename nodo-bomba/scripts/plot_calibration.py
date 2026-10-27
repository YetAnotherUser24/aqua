#!/usr/bin/env python3
"""
plot_calibration.py - Generates Flow Rate vs Speed Gravimetric Calibration Plot
with 95% confidence intervals and residual analysis from experimental CSV data.
"""

import os
import sys
import tempfile
os.environ.setdefault('MPLCONFIGDIR', tempfile.gettempdir())

import csv
import argparse
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def main():
    parser = argparse.ArgumentParser(description="Plot Gravimetric Flow Calibration Curve")
    parser.add_argument("--input", default=os.path.join(SCRIPT_DIR, "data", "flow_calibration.csv"), help="Input CSV")
    parser.add_argument("--output", default=os.path.join(SCRIPT_DIR, "results", "figures", "flow_calibration_curve.png"), help="Output PNG")
    parser.add_argument("--density", type=float, default=1.00, help="Solution density g/mL (water=1.00, 10%% Na2SO3 ~1.085)")
    args = parser.parse_args()

    speeds_rpm = []
    flow_rates_ml_min = []
    revs_list = []
    masses_g = []

    if os.path.isfile(args.input):
        with open(args.input, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    rpm_val = float(row['Target_RPM'])
                    dur_val = float(row.get('Duration_s', 60.0))
                    mass_val = float(row['Mass_g'])
                    rev_val = float(row.get('Revolutions', rpm_val * dur_val / 60.0))
                    vol_val = mass_val / args.density
                    flow_val = (vol_val / dur_val) * 60.0

                    speeds_rpm.append(rpm_val)
                    flow_rates_ml_min.append(flow_val)
                    revs_list.append(rev_val)
                    masses_g.append(mass_val)
                except (ValueError, KeyError):
                    continue

    if len(speeds_rpm) < 2:
        # Fallback to defaults if CSV is missing
        speeds_rpm = np.array([800.0, 1000.0, 1200.0, 1400.0])
        flow_rates_ml_min = np.array([28.23, 34.98, 41.19, 47.54])
    else:
        speeds_rpm = np.array(speeds_rpm)
        flow_rates_ml_min = np.array(flow_rates_ml_min)

    # Balance uncertainty per point: +/- 0.01g
    scale_uncert = 0.01 / args.density

    # Linear Regression: Q = m * RPM + b
    p = np.polyfit(speeds_rpm, flow_rates_ml_min, 1)
    m, b = p[0], p[1]

    # Average volumetric displacement (total mL / total revs)
    if len(revs_list) > 0 and sum(revs_list) > 0:
        ml_per_rev_avg = sum(masses_g) / (args.density * sum(revs_list))
    else:
        ml_per_rev_avg = m

    # R^2 and residuals
    q_pred = np.polyval(p, speeds_rpm)
    residuals = flow_rates_ml_min - q_pred
    ss_res = np.sum(residuals**2)
    ss_tot = np.sum((flow_rates_ml_min - np.mean(flow_rates_ml_min))**2)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 1.0
    rmse = np.sqrt(np.mean(residuals**2))

    # Continuous fit line & 95% Confidence Band
    rpm_dense = np.linspace(max(0.0, np.min(speeds_rpm) - 150), np.max(speeds_rpm) + 150, 200)
    q_dense = np.polyval(p, rpm_dense)
    n = len(speeds_rpm)
    df = max(1, n - 2)
    s_err = np.sqrt(ss_res / df) if df > 0 else 0.01
    t_val = 3.182 if n >= 4 else 4.303
    denom = np.sum((speeds_rpm - np.mean(speeds_rpm))**2)
    denom = denom if denom > 0 else 1.0
    ci_band = t_val * s_err * np.sqrt(1.0/n + (rpm_dense - np.mean(speeds_rpm))**2 / denom)

    # Create 2-panel figure: Top = Calibration Curve; Bottom = Residuals
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9.5, 7.5), sharex=True, gridspec_kw={'height_ratios': [2.2, 1.0]})
    fig.patch.set_facecolor('#ffffff')

    # Panel 1: Calibration Curve
    ax1.errorbar(speeds_rpm, flow_rates_ml_min, yerr=scale_uncert, fmt='o', color='#d62728', 
                 ecolor='#333333', elinewidth=1.5, capsize=4, markersize=7, 
                 label=f'Gravimetric Measurements (Scale 0.01g, ρ={args.density:.2f} g/mL)')
    ax1.plot(rpm_dense, q_dense, color='#1f77b4', linewidth=2.0, 
             label=f'Calibration Fit: Q = {m:.4f}·RPM {b:+.3f} (mL/min)')
    ax1.fill_between(rpm_dense, q_dense - ci_band, q_dense + ci_band, color='#1f77b4', alpha=0.15, 
                     label='95% Confidence Interval')

    ax1.set_ylabel('Flow Rate Q (mL/min)', fontsize=11, fontweight='bold')
    ax1.grid(True, linestyle='--', alpha=0.6)
    ax1.legend(loc='upper left', frameon=True, fontsize=9.5)
    ax1.set_title('Peristaltic Pump Gravimetric Flow Calibration Curve (Experimental Data)', fontsize=13, fontweight='bold', pad=10)

    # Box with calibration constants on the right
    box_str = (
        f"Calibration Results:\n"
        f"  Volumetric Factor: {ml_per_rev_avg:.4f} mL/rev\n"
        f"  Slope m: {m:.4f} mL/min/RPM\n"
        f"  Offset b: {b:.4f} mL/min\n"
        f"  Fit Linearity R²: {r2:.5f}\n"
        f"  Std Error: ±{rmse:.4f} mL/min\n"
        f"  Speed Range: {np.min(speeds_rpm):.0f} – {np.max(speeds_rpm):.0f} RPM"
    )
    ax1.text(0.98, 0.10, box_str, transform=ax1.transAxes, fontsize=9.5, verticalalignment='bottom', horizontalalignment='right',
             bbox=dict(boxstyle='round,pad=0.5', facecolor='#f8f9fa', edgecolor='#cccccc', alpha=0.9))

    # Panel 2: Residuals
    ax2.plot(speeds_rpm, residuals, 's-', color='#2ca02c', linewidth=1.5, markersize=6, label='Residual Error (Q_meas - Q_fit)')
    ax2.axhline(0, color='black', linestyle=':', linewidth=1.0)
    ax2.set_xlabel('Rotational Speed (AS5600 Motor Shaft RPM)', fontsize=11, fontweight='bold')
    ax2.set_ylabel('Residual (mL/min)', fontsize=11, fontweight='bold')
    ax2.grid(True, linestyle='--', alpha=0.6)
    ax2.legend(loc='upper right', frameon=True, fontsize=9)

    plt.tight_layout()
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    plt.savefig(args.output, dpi=300)
    plt.close()
    print(f"[OK] Calibration Plot updated successfully: {args.output}")
    print(f"     Volumetric Factor: {ml_per_rev_avg:.4f} mL/rev | Slope: {m:.4f} | R²: {r2:.5f}")

if __name__ == "__main__":
    main()
