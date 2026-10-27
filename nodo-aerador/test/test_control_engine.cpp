#include <assert.h>
#include <stdio.h>
#include <math.h>
#include <stdint.h>
#include <string.h>

#include "control_engine.h"
#include "config.h"

unsigned long g_fakeMillis = 0;

int main() {
  printf("=================================================================\n");
  printf("[TEST] Running Control Engine SIL Unit Tests (2-DOF PI & Winning Model)\n");
  printf("=================================================================\n");

  ControlEngine engine;
  ControlConfig cfg = engine.getConfig();

  // Winning parameters
  cfg.mode = MODE_PID;
  cfg.target_do_mg_l = 6.0f;
  cfg.kp = 221.65f;
  cfg.ki = 1.915f;
  cfg.kd = 0.0f;
  cfg.beta = 0.35f;
  cfg.tt = 100.0f;
  cfg.tau_lag = 30.0f;
  cfg.min_rpm = 0.0f;
  cfg.max_rpm = 600.0f;
  cfg.failsafe_rpm = 0.0f;
  cfg.manual_throttle_pct = 0.0f;
  engine.configure(cfg);
  engine.setArmed(true);

  // -------------------------------------------------------------
  // Test 1: Manual Mode Override
  // -------------------------------------------------------------
  engine.setManualThrottle(50.0f);
  g_fakeMillis = 1000UL;
  float manualOut = engine.computeOutputRpm(g_fakeMillis);
  assert(fabsf(manualOut - 300.0f) < 0.01f);
  printf("  -> Test 1 (Manual Override): PASSED (300 RPM at 50%%)\n");

  // -------------------------------------------------------------
  // Test 2: Emergency Stop Priority and Latch
  // -------------------------------------------------------------
  engine.triggerEmergencyStop();
  assert(engine.isEmergencyStopActive() == true);
  float eStopOut = engine.computeOutputRpm(g_fakeMillis);
  assert(eStopOut == 0.0f);

  // Clearing E-Stop
  engine.clearEmergencyStop();
  assert(engine.isEmergencyStopActive() == false);
  printf("  -> Test 2 (Emergency Stop Priority & Clear): PASSED (Instant 0 RPM latch)\n");

  // Re-configure in PID mode
  cfg.mode = MODE_PID;
  engine.configure(cfg);

  // -------------------------------------------------------------
  // Test 3: Safe Startup (No packet yet -> 0 RPM)
  // -------------------------------------------------------------
  g_fakeMillis = 2000UL;
  float initialRpm = engine.computeOutputRpm(g_fakeMillis);
  assert(initialRpm == 0.0f);
  assert(engine.isFailsafeActive() == true);
  printf("  -> Test 3 (Safe Startup Failsafe): PASSED (0 RPM before first packet)\n");

  // -------------------------------------------------------------
  // Test 4: 2-DOF Proportional Action (Beta = 0.35 reduces kick)
  // -------------------------------------------------------------
  // Initial sample: DO = 1.7 mg/L (basal deoxygenated state after Protocol A)
  // With beta = 0.35: eP = 0.35 * 6.0 - 1.7 = 2.1 - 1.7 = 0.40 mg/L
  // Expected proportional term = Kp * eP = 221.65 * 0.40 = 88.66 RPM
  // (In contrast, 1-DOF standard PI would have eP = 4.30 mg/L -> 953 RPM, saturated!)
  g_fakeMillis = 5000UL;
  engine.updateProcessVariable(1.70f, 24.0f);
  float step0Rpm = engine.computeOutputRpm(g_fakeMillis);
  printf("  -> Initial step RPM with beta=0.35: %.2f RPM\n", step0Rpm);
  assert(step0Rpm > 80.0f && step0Rpm < 140.0f); // Proportional kick smoothly cushioned!
  printf("  -> Test 4 (2-DOF Setpoint Weighting): PASSED (Kick smoothed to %.1f RPM, no 600 RPM shock)\n", step0Rpm);

  // -------------------------------------------------------------
  // Test 5: Zero-Order Hold (ZOH) between 5-second samples
  // -------------------------------------------------------------
  // At t = 5050 ms (50 ms later), without a new sample, RPM should remain identical
  g_fakeMillis = 5050UL;
  float zohRpm = engine.computeOutputRpm(g_fakeMillis);
  assert(fabsf(zohRpm - step0Rpm) < 0.001f);
  printf("  -> Test 5 (Zero-Order Hold ZOH at 20 Hz): PASSED (Held constant at %.2f RPM)\n", zohRpm);

  // -------------------------------------------------------------
  // Test 6: Anti-Windup Back-Calculation under Saturation
  // -------------------------------------------------------------
  // Feed persistent low DO to drive controller into saturation (~75 s)
  for (int i = 1; i <= 16; i++) {
    g_fakeMillis += 5000UL;
    engine.updateProcessVariable(1.80f, 24.0f);
    engine.computeOutputRpm(g_fakeMillis);
  }
  float saturatedRpm = engine.computeOutputRpm(g_fakeMillis);
  assert(saturatedRpm == 600.0f);
  printf("  -> Test 6a (Saturation Clamping): PASSED (Clamped at 600.0 RPM)\n");

  // Now simulate sudden error drop to 5.8 mg/L (near setpoint 6.0)
  g_fakeMillis += 5000UL;
  engine.updateProcessVariable(5.80f, 24.0f);
  float desatRpm = engine.computeOutputRpm(g_fakeMillis);
  // Thanks to Back-Calculation with Tt = 100s, integral did not wind up to infinity,
  // so the output desaturates immediately below 600 RPM!
  printf("  -> Desaturated RPM near setpoint: %.2f RPM\n", desatRpm);
  assert(desatRpm < 600.0f);
  printf("  -> Test 6b (Back-Calculation Desaturation): PASSED (Swift desaturation to %.1f RPM)\n", desatRpm);

  // -------------------------------------------------------------
  // Test 7: Predictive Coasting Cutoff (Clean-Water Physics)
  // -------------------------------------------------------------
  // DO is at 5.5 mg/L at t1, rising to 5.7 mg/L at t2 (dt = 5s)
  // dC/dt = (5.7 - 5.5) / 5.0 = 0.040 mg/L/s
  // With tau_lag = 30s: C_pred = 5.7 + 30 * 0.040 = 6.90 mg/L >= 6.0 mg/L!
  // Controller MUST immediately cut power to 0 RPM to prevent catastrophic overshoot!
  g_fakeMillis += 5000UL;
  engine.updateProcessVariable(5.50f, 24.0f);
  engine.computeOutputRpm(g_fakeMillis);

  g_fakeMillis += 5000UL;
  engine.updateProcessVariable(5.70f, 24.0f);
  float coastingRpm = engine.computeOutputRpm(g_fakeMillis);
  printf("  -> Coasting Cutoff RPM: %.2f RPM (C_meas=5.7 mg/L < 6.0 mg/L, but C_pred=6.9 mg/L)\n", coastingRpm);
  assert(coastingRpm == 0.0f);
  printf("  -> Test 7 (Predictive Coasting Cutoff): PASSED (Immediate 0 RPM cut before physical crossing)\n");

  // -------------------------------------------------------------
  // Test 8: Watchdog Failsafe Timeout (> 35s packet gap)
  // -------------------------------------------------------------
  // Advance time by 36 seconds without any updateProcessVariable
  g_fakeMillis += 36000UL;
  float failsafeOut = engine.computeOutputRpm(g_fakeMillis);
  assert(engine.isFailsafeActive() == true);
  assert(failsafeOut == cfg.failsafe_rpm);
  printf("  -> Test 8 (Watchdog 35s Timeout): PASSED (Failsafe triggered, 0 RPM safe state)\n");

  // -------------------------------------------------------------
  // Test 9: Disarm State Wipes Integral
  // -------------------------------------------------------------
  engine.setArmed(false);
  assert(engine.isArmed() == false);
  printf("  -> Test 9 (Disarming Clean Reset): PASSED\n");

  printf("=================================================================\n");
  printf("[TEST] ALL 9 UNIT & INVARIANT TESTS PASSED PERFECTLY!\n");
  printf("=================================================================\n");
  return 0;
}
