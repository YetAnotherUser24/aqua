#pragma once
/**
 * @file vibration_processor.h
 * @brief Calculo y procesamiento estadistico de vibracion (RMS, Peak, Crest Factor).
 */

#include "vibration_bus.h"
#include "config.h"
#include <stddef.h>

struct VibrationTelemetry {
  float ax_g = 0.0f;
  float ay_g = 0.0f;
  float az_g = 0.0f;
  float rms_g = 0.0f;
  float peak_g = 0.0f;
  float crest_factor = 0.0f;
  float temp_c = 0.0f;
  int dev_status = 0;
  size_t sample_count = 0;
};

class VibrationProcessor {
public:
  VibrationProcessor();

  /**
   * @brief Agrega una muestra leida del sensor a la ventana de acumulacion.
   */
  void addSample(const VibrationSample& sample);

  /**
   * @brief Procesa la ventana acumulada, calcula metricas (RMS, Peak, etc.)
   *        y reinicia el acumulador para la siguiente ventana.
   * @param out Estructura donde se escriben los resultados listos para telemetria.
   * @return true si habia muestras suficientes para un calculo valido.
   */
  bool finalizeWindow(VibrationTelemetry& out);

  /**
   * @brief Numero de muestras acumuladas en la ventana actual.
   */
  size_t sampleCount() const { return _count; }

  /**
   * @brief Reinicia el acumulador.
   */
  void reset();

private:
  size_t _count;
  float _sumAx;
  float _sumAy;
  float _sumAz;
  float _sumTemp;

  // Arreglos circulares o buffers de ventana para el calculo de componentes dinamicas
  float _samplesAx[MAX_WINDOW_SAMPLES];
  float _samplesAy[MAX_WINDOW_SAMPLES];
  float _samplesAz[MAX_WINDOW_SAMPLES];
};
