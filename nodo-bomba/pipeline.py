#!/usr/bin/env python3
"""
pipeline.py
Peristaltic Dosing Pump Controller Pipeline Manager (AquaControl V4).
Automates the full experimental engineering workflow:
  1. Data Acquisition (Staircase Return-to-Zero, PRBS, or Step)
  2. Plant Identification & PI Tuning (Python-native FOPDT/SIMC/ITAE or MATLAB PSO)
  3. Closed-Loop Validation Run
  4. Flow vs RPM Gravimetric Calibration
"""

import argparse
import subprocess
import sys
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def run_cmd(cmd):
    print("==================================================")
    print(f"RUNNING: {cmd}")
    print("==================================================")
    result = subprocess.run(cmd, shell=True, cwd=BASE_DIR)
    if result.returncode != 0:
        print(f"\n[ERROR] Command failed with exit code {result.returncode}: {cmd}")
        sys.exit(1)

def get_gains(filepath):
    full_path = os.path.join(BASE_DIR, filepath) if not os.path.isabs(filepath) else filepath
    if not os.path.exists(full_path):
        print(f"\n[ERROR] {full_path} not found. Run the 'ident' or 'ident-py' step first!")
        sys.exit(1)
    with open(full_path, 'r') as f:
        for line in f:
            if line.startswith('--kp'):
                return line.strip()
    print(f"\n[ERROR] Could not find gains inside {full_path}")
    sys.exit(1)

# Python interpreter with scipy/matplotlib/numpy
PY_BIN = "/root/miniforge3/envs/pyorc_env/bin/python" if os.path.isfile("/root/miniforge3/envs/pyorc_env/bin/python") else sys.executable

def do_data(args):
    port_arg = f"--port {args.port}" if args.port else ""
    if args.method == 'prbs':
        print("\n>>> STEP 1: DATA ACQUISITION (PRBS) <<<")
        run_cmd(f"{PY_BIN} scripts/sysid_prbs_logger.py {port_arg} --duration {args.duration} --step_time 3.0 --min_duty {args.min_duty} --max_duty {args.max_duty}")
    elif args.method == 'stair':
        print("\n>>> STEP 1: DATA ACQUISITION (STAIRCASE RETURN-TO-ZERO) <<<")
        run_cmd(f"{PY_BIN} scripts/sysid_stair_logger.py {port_arg} --step_time 4.0")
    else:
        print("\n>>> STEP 1: DATA ACQUISITION (STEP) <<<")
        run_cmd(f"{PY_BIN} scripts/sysid_logger.py {port_arg} --duty {args.duty} --duration {args.duration}")

def do_ident_py():
    print("\n>>> STEP 2: SYSTEM IDENTIFICATION & TUNING (PYTHON FOPDT/SIMC/ITAE) <<<")
    run_cmd(f"{PY_BIN} scripts/identify_and_tune.py --input scripts/data/step_response.csv")
    do_plot_sysid()

def do_ident_pso():
    print("\n>>> STEP 2: SYSTEM IDENTIFICATION & TUNING (MATLAB PSO) <<<")
    run_cmd("matlab -batch \"cd scripts; run('advanced_thesis_tuning.m')\"")

def do_ident_pidtune():
    print("\n>>> STEP 2: SYSTEM IDENTIFICATION & TUNING (MATLAB PIDTUNE) <<<")
    run_cmd("matlab -batch \"cd scripts; run('sysid_and_tune.m')\"")

def do_run(args, model_type='py'):
    print(f"\n>>> STEP 3: RUN CLOSED-LOOP VALIDATION ({model_type.upper()}) <<<")
    port_arg = f"--port {args.port}" if args.port else ""
    gain_file = "scripts/results/models/pidtune_gains.txt" if model_type == 'py' else "scripts/results/models/pso_gains.txt"
    gains = get_gains(gain_file)
    run_cmd(f"{PY_BIN} scripts/test_gains.py {gains} --rpm {args.rpm} {port_arg} --output scripts/data/closed_loop_{model_type}.csv")
    do_plot_val(f"scripts/data/closed_loop_{model_type}.csv")

def do_calib(args):
    print("\n>>> STEP 4: GRAVIMETRIC FLOW CALIBRATION (Q vs RPM) <<<")
    port_arg = f"--port {args.port}" if args.port else ""
    run_cmd(f"{PY_BIN} scripts/calibrate_flow.py {port_arg} --density {args.density} --duration {args.calib_duration}")
    do_plot_calib(args.density)

def do_plot_sysid():
    print("\n>>> GENERATING SYSTEM IDENTIFICATION FIGURE <<<")
    run_cmd(f"{PY_BIN} scripts/plot_sysid.py")

def do_plot_val(csv_file="scripts/data/closed_loop_py.csv"):
    print("\n>>> GENERATING CLOSED-LOOP VALIDATION FIGURE <<<")
    run_cmd(f"{PY_BIN} scripts/plot_validation.py --input {csv_file}")

def do_plot_calib(density=1.085):
    print("\n>>> GENERATING FLOW CALIBRATION FIGURE <<<")
    run_cmd(f"{PY_BIN} scripts/plot_calibration.py --density {density}")

def do_plot_dose():
    print("\n>>> GENERATING CHEMICAL DOSING CYCLE PROFILE FIGURE <<<")
    run_cmd(f"{PY_BIN} scripts/plot_dosing_profile.py")

def do_plot_all():
    do_plot_sysid()
    do_plot_val()
    do_plot_calib()
    do_plot_dose()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Peristaltic Dosing Pump Controller Pipeline Manager")
    parser.add_argument('action', choices=[
        'full',
        'full-py',
        'data',
        'ident',
        'ident-py',
        'ident-pso',
        'ident-pidtune',
        'run',
        'run-py',
        'run-pso',
        'calib',
        'plot',
        'plot-sysid',
        'plot-val',
        'plot-calib',
        'plot-dose'
    ], help="The action to perform in the pipeline.")
    parser.add_argument('--method', type=str, choices=['step', 'prbs', 'stair'], default='stair', help='Data acquisition method (default: stair)')
    parser.add_argument('--port', type=str, default='', help='Serial port (e.g. /dev/ttyACM0 or COM3). Auto-detected if omitted.')
    parser.add_argument('--duty', type=float, default=0.6, help='Duty cycle for open-loop sysid (STEP method)')
    parser.add_argument('--min_duty', type=float, default=0.2, help='Min duty for PRBS method')
    parser.add_argument('--max_duty', type=float, default=0.8, help='Max duty for PRBS method')
    parser.add_argument('--rpm', type=float, default=60.0, help='Target RPM for closed-loop validation test')
    parser.add_argument('--duration', type=float, default=45.0, help='Duration of data acquisition in seconds')
    parser.add_argument('--calib_duration', type=float, default=60.0, help='Duration per RPM step in calibration')
    parser.add_argument('--density', type=float, default=1.085, help='Solution density in g/mL (water=1.00, 10%% Na2SO3 ~1.085)')

    args = parser.parse_args()

    if args.action == 'data':
        do_data(args)
    elif args.action == 'ident-py':
        do_ident_py()
    elif args.action == 'ident':
        do_ident_py()
        if os.system("which matlab > /dev/null 2>&1") == 0:
            do_ident_pso()
    elif args.action == 'ident-pso':
        do_ident_pso()
    elif args.action == 'ident-pidtune':
        do_ident_pidtune()
    elif args.action == 'run-py' or args.action == 'run':
        do_run(args, 'py')
    elif args.action == 'run-pso':
        do_run(args, 'pso')
    elif args.action == 'calib':
        do_calib(args)
    elif args.action == 'plot' or args.action == 'plot-all':
        do_plot_all()
    elif args.action == 'plot-sysid':
        do_plot_sysid()
    elif args.action == 'plot-val':
        do_plot_val()
    elif args.action == 'plot-calib':
        do_plot_calib(args.density)
    elif args.action == 'plot-dose':
        do_plot_dose()
    elif args.action == 'full-py':
        do_data(args)
        do_ident_py()
        do_run(args, 'py')
    elif args.action == 'full':
        do_data(args)
        do_ident_py()
        if os.system("which matlab > /dev/null 2>&1") == 0:
            do_ident_pso()
        do_run(args, 'py')
