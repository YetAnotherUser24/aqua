#!/usr/bin/env python3
"""
pipeline.py
Peristaltic Dosing Pump Controller Pipeline Manager (AquaControl V4).
Automates the full experimental engineering workflow:
  1. Data Acquisition (Staircase Return-to-Zero, PRBS, or Step)
  2. Plant Identification & PI Tuning (Python-native FOPDT/SIMC/ITAE or MATLAB PSO)
  3. Closed-Loop Validation Run & Performance Metrics
  4. Flow vs RPM Gravimetric Calibration & Dosing Profiling
"""

import argparse
import subprocess
import sys
import os
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def run_cmd(cmd, check=True):
    print("==================================================")
    print(f"RUNNING: {cmd}")
    print("==================================================")
    result = subprocess.run(cmd, shell=True, cwd=BASE_DIR)
    if result.returncode != 0:
        if check:
            print(f"\n[ERROR] Command failed with exit code {result.returncode}: {cmd}")
            sys.exit(1)
        else:
            print(f"\n[WARNING] Command returned exit code {result.returncode}: {cmd}")
            return False
    return True

def get_gains(filepath):
    full_path = os.path.join(BASE_DIR, filepath) if not os.path.isabs(filepath) else filepath
    if not os.path.exists(full_path):
        print(f"\n[ERROR] {full_path} not found. Run the 'ident' step first!")
        sys.exit(1)
    with open(full_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith('--kp'):
                return line
            # Handle comma-separated format: Kp,Ki,Kd
            parts = line.split(',')
            if len(parts) >= 2:
                try:
                    kp = float(parts[0])
                    ki = float(parts[1])
                    kd = float(parts[2]) if len(parts) > 2 else 0.0
                    return f"--kp {kp:.6f} --ki {ki:.6f} --kd {kd:.6f}"
                except ValueError:
                    pass
    print(f"\n[ERROR] Could not parse gains inside {full_path}")
    sys.exit(1)

def has_matlab():
    return shutil.which("matlab") is not None

PY_BIN = sys.executable

def do_data(args):
    port_arg = f"--port {args.port}" if args.port else ""
    if args.method == 'prbs':
        print("\n>>> STEP 1: DATA ACQUISITION (PRBS) <<<")
        run_cmd(f'"{PY_BIN}" scripts/sysid_prbs_logger.py {port_arg} --duration {args.duration} --step_time 3.0 --min_duty {args.min_duty} --max_duty {args.max_duty}')
    elif args.method == 'stair':
        print("\n>>> STEP 1: DATA ACQUISITION (STAIRCASE RETURN-TO-ZERO) <<<")
        run_cmd(f'"{PY_BIN}" scripts/sysid_stair_logger.py {port_arg} --step_time 4.0')
    else:
        print("\n>>> STEP 1: DATA ACQUISITION (STEP) <<<")
        run_cmd(f'"{PY_BIN}" scripts/sysid_logger.py {port_arg} --duty {args.duty} --duration {args.duration}')

def do_ident_py(data_file='scripts/data/step_response.csv'):
    print("\n>>> STEP 2: SYSTEM IDENTIFICATION & TUNING (PYTHON FOPDT/SIMC/ITAE) <<<")
    # Check if staircase data exists as fallback
    if not os.path.exists(os.path.join(BASE_DIR, data_file)) and os.path.exists(os.path.join(BASE_DIR, 'scripts/data/test_stair.csv')):
        data_file = 'scripts/data/test_stair.csv'
    run_cmd(f'"{PY_BIN}" scripts/identify_and_tune.py --input {data_file}')
    do_plot_sysid(data_file)

def do_ident_pso():
    print("\n>>> STEP 2: SYSTEM IDENTIFICATION & TUNING (MATLAB PSO) <<<")
    if not has_matlab():
        print("[WARNING] MATLAB not found on system PATH. Falling back to Python optimizer.")
        do_ident_py()
        return
    ok = run_cmd('matlab -batch "cd scripts; run(\'advanced_thesis_tuning.m\')"', check=False)
    if not ok:
        print("[WARNING] MATLAB PSO execution returned an error. Using Python/existing models.")

def do_ident_pidtune():
    print("\n>>> STEP 2: SYSTEM IDENTIFICATION & TUNING (MATLAB PIDTUNE) <<<")
    if not has_matlab():
        print("[WARNING] MATLAB not found on system PATH. Falling back to Python optimizer.")
        do_ident_py()
        return
    ok = run_cmd('matlab -batch "cd scripts; run(\'sysid_and_tune.m\')"', check=False)
    if not ok:
        print("[WARNING] MATLAB pidtune execution returned an error. Using Python/existing models.")

def do_run_py(args):
    print("\n>>> STEP 3: RUN CLOSED-LOOP VALIDATION (PYTHON SIMC) <<<")
    port_arg = f"--port {args.port}" if args.port else ""
    gains = get_gains("scripts/results/models/py_gains.txt" if os.path.exists(os.path.join(BASE_DIR, "scripts/results/models/py_gains.txt")) else "scripts/results/models/pidtune_gains.txt")
    out_csv = "scripts/data/closed_loop_py.csv"
    run_cmd(f'"{PY_BIN}" scripts/test_gains.py {gains} --rpm {args.rpm} {port_arg} --output {out_csv}')
    do_plot_val(out_csv, "scripts/results/figures/closed_loop_validation.png")

def do_run_pso(args):
    print("\n>>> STEP 3: RUN CLOSED-LOOP VALIDATION (PSO) <<<")
    port_arg = f"--port {args.port}" if args.port else ""
    gains = get_gains("scripts/results/models/pso_gains.txt")
    out_csv = "scripts/data/closed_loop_pso.csv"
    run_cmd(f'"{PY_BIN}" scripts/test_gains.py {gains} --rpm {args.rpm} {port_arg} --output {out_csv}')
    do_plot_val(out_csv, "scripts/results/figures/pso/closed_loop_validation.png")

def do_run_pidtune(args):
    print("\n>>> STEP 3: RUN CLOSED-LOOP VALIDATION (PIDTUNE) <<<")
    port_arg = f"--port {args.port}" if args.port else ""
    gains = get_gains("scripts/results/models/pidtune_gains.txt")
    out_csv = "scripts/data/closed_loop_pidtune.csv"
    run_cmd(f'"{PY_BIN}" scripts/test_gains.py {gains} --rpm {args.rpm} {port_arg} --output {out_csv}')
    do_plot_val(out_csv, "scripts/results/figures/pidtune/closed_loop_validation.png")

def do_sweep(args):
    print("\n>>> STEP: MULTI-SPEED STAIRCASE CLOSED-LOOP SWEEP <<<")
    port_arg = f"--port {args.port}" if args.port else ""
    run_cmd(f'"{PY_BIN}" scripts/test_speed_sweep.py {port_arg} --speeds {args.speeds} --step_time {args.step_time}')

def do_calib(args):
    print("\n>>> STEP 4: GRAVIMETRIC FLOW CALIBRATION (Q vs RPM) <<<")
    port_arg = f"--port {args.port}" if args.port else ""
    rpms_arg = f"--rpms {args.rpms}" if (hasattr(args, 'rpms') and args.rpms) else ""
    run_cmd(f'"{PY_BIN}" scripts/calibrate_flow.py {port_arg} {rpms_arg} --density {args.density} --duration {args.calib_duration}')
    do_plot_calib(args.density)

def do_simulate(args=None):
    print("\n>>> STEP: CLOSED-LOOP SIMULATION & BENCHMARK OF ALL CONTROLLERS <<<")
    rpm_val = args.rpm if (args and hasattr(args, 'rpm') and args.rpm) else 1000.0
    run_cmd(f'"{PY_BIN}" scripts/simulate_controllers.py --rpm {rpm_val}')

def do_ident_all(args=None):
    print("\n==============================================================")
    print(">>> RUNNING ALL SYSTEM IDENTIFICATION & TUNING PIPELINES <<<")
    print("==============================================================")
    do_ident_py()
    if has_matlab():
        do_ident_pidtune()
        do_ident_pso()
    else:
        print("[INFO] MATLAB not detected on PATH. Python SIMC & ITAE models are ready.")
    do_simulate(args)

def do_plot_sysid(input_csv="scripts/data/step_response.csv"):
    print("\n>>> GENERATING SYSTEM IDENTIFICATION FIGURE <<<")
    if not os.path.exists(os.path.join(BASE_DIR, input_csv)) and os.path.exists(os.path.join(BASE_DIR, "scripts/data/test_stair.csv")):
        input_csv = "scripts/data/test_stair.csv"
    run_cmd(f'"{PY_BIN}" scripts/plot_sysid.py --input {input_csv}')

def do_plot_val(csv_file="scripts/data/closed_loop_py.csv", out_png="scripts/results/figures/closed_loop_validation.png"):
    print(f"\n>>> GENERATING CLOSED-LOOP VALIDATION FIGURE ({os.path.basename(csv_file)}) <<<")
    os.makedirs(os.path.dirname(os.path.join(BASE_DIR, out_png)), exist_ok=True)
    run_cmd(f'"{PY_BIN}" scripts/plot_validation.py --input {csv_file} --output {out_png}')

def do_plot_calib(density=1.085):
    print("\n>>> GENERATING FLOW CALIBRATION FIGURE <<<")
    run_cmd(f'"{PY_BIN}" scripts/plot_calibration.py --density {density}')

def do_plot_dose():
    print("\n>>> GENERATING CHEMICAL DOSING CYCLE PROFILE FIGURE <<<")
    run_cmd(f'"{PY_BIN}" scripts/plot_dosing_profile.py')

def do_plot_all(args=None):
    do_plot_sysid()
    if os.path.exists(os.path.join(BASE_DIR, "scripts/data/closed_loop_py.csv")):
        do_plot_val("scripts/data/closed_loop_py.csv", "scripts/results/figures/closed_loop_validation.png")
    if os.path.exists(os.path.join(BASE_DIR, "scripts/data/closed_loop_pso.csv")):
        do_plot_val("scripts/data/closed_loop_pso.csv", "scripts/results/figures/pso/closed_loop_validation.png")
    if os.path.exists(os.path.join(BASE_DIR, "scripts/data/closed_loop_pidtune.csv")):
        do_plot_val("scripts/data/closed_loop_pidtune.csv", "scripts/results/figures/pidtune/closed_loop_validation.png")
    do_plot_calib()
    do_plot_dose()
    do_simulate(args)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Peristaltic Dosing Pump Controller Pipeline Manager")
    parser.add_argument('action', choices=[
        'full',
        'full-all',
        'full-py',
        'full-pso',
        'full-pidtune',
        'data',
        'ident',
        'ident-all',
        'ident-py',
        'ident-pso',
        'ident-pidtune',
        'apply',
        'apply-best',
        'apply-pso',
        'apply-pidtune',
        'apply-py',
        'run',
        'run-best',
        'run-py',
        'run-pso',
        'run-pidtune',
        'simulate',
        'sim',
        'compare',
        'sweep',
        'calib',
        'plot',
        'plot-all',
        'plot-sysid',
        'plot-val',
        'plot-calib',
        'plot-dose'
    ], help="The action to perform in the pipeline.")
    parser.add_argument('--method', type=str, choices=['step', 'prbs', 'stair'], default='stair', help='Data acquisition method (default: stair)')
    parser.add_argument('--port', type=str, default='', help='Serial port (e.g. COM3 or /dev/ttyACM0). Auto-detected if omitted.')
    parser.add_argument('--duty', type=float, default=0.6, help='Duty cycle for open-loop sysid (STEP method)')
    parser.add_argument('--min_duty', type=float, default=0.2, help='Min duty for PRBS method')
    parser.add_argument('--max_duty', type=float, default=0.8, help='Max duty for PRBS method')
    parser.add_argument('--rpm', type=float, default=1000.0, help='Target RPM for closed-loop validation test (default: 1000.0)')
    parser.add_argument('--rpms', type=str, default='600,800,1000,1200,1400', help='Comma-separated RPM test points for calibration (default: 600,800,1000,1200,1400)')
    parser.add_argument('--speeds', type=str, default='400,800,1200,1600,2000', help='Comma-separated RPM test points for sweep')
    parser.add_argument('--step_time', type=float, default=4.0, help='Duration in seconds per speed step in sweep')
    parser.add_argument('--duration', type=float, default=45.0, help='Duration of data acquisition in seconds')
    parser.add_argument('--calib_duration', type=float, default=60.0, help='Duration per RPM step in calibration')
    parser.add_argument('--density', type=float, default=1.085, help='Solution density in g/mL (water=1.00, 10%% Na2SO3 ~1.085)')

    args = parser.parse_args()

    if args.action == 'data':
        do_data(args)

    elif args.action == 'ident-py':
        do_ident_py()

    elif args.action == 'ident-pidtune':
        do_ident_pidtune()

    elif args.action == 'ident-pso':
        do_ident_pso()

    elif args.action in ('ident', 'ident-all'):
        do_ident_all(args)

    elif args.action in ('simulate', 'sim', 'compare'):
        do_simulate(args)

    elif args.action == 'sweep':
        do_sweep(args)

    elif args.action in ('run-py', 'run-best'):
        do_run_py(args)

    elif args.action == 'run-pso':
        do_run_pso(args)

    elif args.action == 'run-pidtune':
        do_run_pidtune(args)

    elif args.action == 'run':
        do_run_py(args)
        if os.path.exists(os.path.join(BASE_DIR, "scripts/results/models/pso_gains.txt")):
            do_run_pso(args)
        if os.path.exists(os.path.join(BASE_DIR, "scripts/results/models/pidtune_gains.txt")):
            do_run_pidtune(args)

    elif args.action == 'apply':
        do_ident_all(args)
        do_run_py(args)
        if has_matlab():
            do_run_pso(args)

    elif args.action in ('apply-py', 'apply-best'):
        do_ident_py()
        do_run_py(args)

    elif args.action == 'apply-pso':
        do_ident_pso()
        do_run_pso(args)

    elif args.action == 'apply-pidtune':
        do_ident_pidtune()
        do_run_pidtune(args)

    elif args.action == 'full-py':
        do_data(args)
        do_ident_py()
        do_run_py(args)
        do_plot_all(args)

    elif args.action == 'full-pso':
        do_data(args)
        do_ident_pso()
        do_run_pso(args)
        do_plot_all(args)

    elif args.action == 'full-pidtune':
        do_data(args)
        do_ident_pidtune()
        do_run_pidtune(args)
        do_plot_all(args)

    elif args.action in ('full', 'full-all'):
        do_data(args)
        do_ident_all(args)
        do_run_py(args)
        if has_matlab():
            do_run_pso(args)
        do_plot_all(args)

    elif args.action == 'calib':
        do_calib(args)

    elif args.action in ('plot', 'plot-all'):
        do_plot_all(args)

    elif args.action == 'plot-sysid':
        do_plot_sysid()

    elif args.action == 'plot-val':
        do_plot_val()

    elif args.action == 'plot-calib':
        do_plot_calib(args.density)

    elif args.action == 'plot-dose':
        do_plot_dose()
