/**
 * @file vibration_processor.cpp
 * @brief Implementacion del procesador de senal de vibracion.
 */

#include "vibration_processor.h"
#include <math.h>

VibrationProcessor::VibrationProcessor() {
  reset();
}

void VibrationProcessor::reset() {
  _count = 0;
  _sumAx = 0.0f;
  _sumAy = 0.0f;
  _sumAz = 0.0f;
  _sumTemp = 0.0f;
}

void VibrationProcessor::addSample(const VibrationSample& sample) {
  if (!sample.valid || _count >= MAX_WINDOW_SAMPLES) {
    return;
  }

  _samplesAx[_count] = sample.ax_g;
  _samplesAy[_count] = sample.ay_g;
  _samplesAz[_count] = sample.az_g;

  _sumAx += sample.ax_g;
  _sumAy += sample.ay_g;
  _sumAz += sample.az_g;
  _sumTemp += sample.temp_c;

  _count++;
}

bool VibrationProcessor::finalizeWindow(VibrationTelemetry& out) {
  if (_count == 0) {
    return false;
  }

  const float n = (float)_count;
  const float meanAx = _sumAx / n;
  const float meanAy = _sumAy / n;
  const float meanAz = _sumAz / n;

  float sumSqDyn = 0.0f;
  float peakDyn = 0.0f;

  for (size_t i = 0; i < _count; ++i) {
    // Componente dinamica (restando la inclinacion / gravedad estatica):
    float dAx = _samplesAx[i] - meanAx;
    float dAy = _samplesAy[i] - meanAy;
    float dAz = _samplesAz[i] - meanAz;

    float magSq = (dAx * dAx) + (dAy * dAy) + (dAz * dAz);
    float mag = sqrtf(magSq);

    sumSqDyn += magSq;
    if (mag > peakDyn) {
      peakDyn = mag;
    }
  }

  float rms = sqrtf(sumSqDyn / n);
  float crestFactor = (rms > 0.001f) ? (peakDyn / rms) : 1.0f;

  // Llenar telemetria
  out.ax_g = meanAx;
  out.ay_g = meanAy;
  out.az_g = meanAz;
  out.rms_g = rms;
  out.peak_g = peakDyn;
  out.crest_factor = crestFactor;
  out.temp_c = _sumTemp / n;
  out.sample_count = _count;

  // Reiniciar ventana para el siguiente ciclo
  reset();
  return true;
}
