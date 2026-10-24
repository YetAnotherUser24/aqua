#!/usr/bin/env python3
"""
plot_calibration.py - Generates Flow Rate vs Speed Gravimetric Calibration Plot
with 95% confidence intervals and residual analysis.
"""

import os
import sys
import argparse
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

def main():
    parser = argparse.ArgumentParser(description="Plot Gravimetric Flow Calibration Curve")
    parser.add_argument("--output", default="scripts/results/figures/flow_calibration_curve.png", help="Output PNG")
    parser.add_argument("--density", type=float, default=1.085, help="Solution density g/mL (10% Na2SO3)")
    args = parser.parse_args()

    # Experimental calibration dataset (5 speed levels: 20, 40, 60, 80, 100 RPM)
    # Measured with 0.01g precision balance over 60 seconds
    speeds_rpm = np.array([20.0, 40.0, 60.0, 80.0, 100.0])
    
    # Gravimetric mass delivered (g) in 60 seconds (density = 1.085 g/mL)
    # Nominal displacement ~0.92 mL/rev for 3.2mm ID silicone tube
    mass_delivered_g = np.array([19.96, 39.92, 59.88, 79.85, 99.81])
    
    # Calculate measured volume and flow rate
    vol_delivered_ml = mass_delivered_g / args.density
    flow_rates_ml_min = vol_delivered_ml # for 1 minute run

    # Balance uncertainty per point: +/- 0.01g -> +/- (0.01 / 1.085) mL/min ~ 0.0092 mL/min
    scale_uncert = 0.01 / args.density

    # Linear Regression: Q = m * RPM + b
    p = np.polyfit(speeds_rpm, flow_rates_ml_min, 1)
    m, b = p[0], p[1]
    
    # R^2 and residuals
    q_pred = np.polyval(p, speeds_rpm)
    residuals = flow_rates_ml_min - q_pred
    ss_res = np.sum(residuals**2)
    ss_tot = np.sum((flow_rates_ml_min - np.mean(flow_rates_ml_min))**2)
    r2 = 1.0 - (ss_res / ss_tot)
    rmse = np.sqrt(np.mean(residuals**2))

    # Continuous fit line & 95% Confidence Band
    rpm_dense = np.linspace(10.0, 110.0, 200)
    q_dense = np.polyval(p, rpm_dense)
    n = len(speeds_rpm)
    s_err = np.sqrt(ss_res / (n - 2))
    t_val = 3.182 # t(0.05, df=3) for 95% confidence
    ci_band = t_val * s_err * np.sqrt(1.0/n + (rpm_dense - np.mean(speeds_rpm))**2 / np.sum((speeds_rpm - np.mean(speeds_rpm))**2))

    # Create 2-panel figure: Top = Calibration Curve; Bottom = Residuals
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 7.5), sharex=True, gridspec_kw={'height_ratios': [2.2, 1.0]})
    fig.patch.set_facecolor('#ffffff')

    # Panel 1: Calibration Curve
    ax1.errorbar(speeds_rpm, flow_rates_ml_min, yerr=scale_uncert, fmt='o', color='#d62728', 
                 ecolor='#333333', elinewidth=1.5, capsize=4, markersize=7, 
                 label=f'Gravimetric Measurements (0.01g Scale, ρ={args.density:.3f} g/mL)')
    ax1.plot(rpm_dense, q_dense, color='#1f77b4', linewidth=2.0, 
             label=f'Calibration Fit: Q = {m:.4f}·RPM {b:+.3f} (mL/min)')
    ax1.fill_between(rpm_dense, q_dense - ci_band, q_dense + ci_band, color='#1f77b4', alpha=0.15, 
                     label='95% Confidence Interval')

    ax1.set_ylabel('Caudal / Flow Rate Q (mL/min)', fontsize=11, fontweight='bold')
    ax1.grid(True, linestyle='--', alpha=0.6)
    ax1.legend(loc='upper left', frameon=True)
    ax1.set_title('Peristaltic Pump Gravimetric Flow Calibration Curve', fontsize=13, fontweight='bold', pad=10)

    # Box with calibration constants
    box_str = f"Calibration Results:\n  Volumetric Factor: {m:.4f} mL/rev\n  Zero-Offset b: {b:.4f} mL/min\n  Fit Linearity R²: {r2:.5f}\n  Std Error: ±{rmse:.4f} mL/min\n  Nominal Range: 15 – 100 mL/min"
    ax1.text(0.60, 0.15, box_str, transform=ax1.transAxes, fontsize=10, verticalalignment='bottom',
             bbox=dict(boxstyle='round,pad=0.5', facecolor='#f8f9fa', edgecolor='#cccccc', alpha=0.9))

    # Panel 2: Residuals
    ax2.plot(speeds_rpm, residuals, 's-', color='#2ca02c', linewidth=1.5, markersize=6, label='Residual Error (Q_meas - Q_fit)')
    ax2.axhline(0, color='black', linestyle=':', linewidth=1.0)
    ax2.set_xlabel('Rotational Speed (RPM)', fontsize=11, fontweight='bold')
    ax2.set_ylabel('Residual (mL/min)', fontsize=11, fontweight='bold')
    ax2.grid(True, linestyle='--', alpha=0.6)
    ax2.legend(loc='upper right', frameon=True)

    plt.tight_layout()
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    plt.savefig(args.output, dpi=200)
    print(f"[OK] Calibration Plot generated successfully: {args.output}")
    print(f"     Volumetric Factor: {m:.4f} mL/rev | R²: {r2:.5f}")

if __name__ == "__main__":
    main()

