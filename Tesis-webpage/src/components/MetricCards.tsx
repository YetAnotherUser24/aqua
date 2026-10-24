'use client';

import React from 'react';
import {
  Droplets,
  Percent,
  Thermometer,
  BatteryCharging,
  Fan,
  Gauge,
  Clock,
  AlertTriangle,
  CheckCircle2,
  AlertOctagon
} from 'lucide-react';
import { SensorReading, MotorTelemetry, SystemThresholds, Device } from '@/types';

interface MetricCardsProps {
  reading: SensorReading | null;
  motor: MotorTelemetry | null;
  thresholds: SystemThresholds;
  lastUpdated: string;
  motorDevice?: Device | null;
  sensorDevice?: Device | null;
}

export const MetricCards: React.FC<MetricCardsProps> = ({
  reading,
  motor,
  thresholds,
  lastUpdated,
  motorDevice,
  sensorDevice
}) => {
  const isSensorOnline = sensorDevice?.status === 'online';
  const isMotorOnline = motorDevice?.status === 'online';

  const doVal = reading?.dissolved_oxygen_mg_l ?? 0;
  const satVal = reading?.oxygen_saturation_pct ?? 0;
  const tempVal = reading?.water_temperature_c ?? 0;
  const batteryV = reading?.battery_v ?? 0;

  const isMotorOn = isMotorOnline && Boolean(motor?.is_on);
  const motorSpeed = isMotorOn ? (motor?.speed_percent ?? 0) : 0;
  const calculatedRpm = Math.round((motorSpeed / 100) * 600);
  const motorPwm = isMotorOnline ? (motor?.pwm_us ?? 1500) : 1500;
  const motorPower = isMotorOnline ? (motor?.power_w ?? (motorSpeed > 0 ? (motorSpeed * 1.9).toFixed(1) : 0)) : 0;

  // Semáforo de calidad de Oxígeno Disuelto
  let doStatus: 'optimal' | 'warning' | 'critical' = 'optimal';
  let doBadgeColor = 'text-emerald-400 bg-emerald-950/70 border-emerald-800';
  let doStatusText = 'Nivel Óptimo';
  let doIcon = <CheckCircle2 className="w-4 h-4 text-emerald-400" />;

  if (!isSensorOnline) {
    doStatus = 'warning';
    doBadgeColor = 'text-amber-400 bg-amber-950/70 border-amber-800';
    doStatusText = 'Sensor Offline';
    doIcon = <AlertTriangle className="w-4 h-4 text-amber-400" />;
  } else if (doVal < thresholds.critical) {
    doStatus = 'critical';
    doBadgeColor = 'text-rose-400 bg-rose-950/80 border-rose-800 animate-pulse';
    doStatusText = 'Nivel Crítico';
    doIcon = <AlertOctagon className="w-4 h-4 text-rose-400" />;
  } else if (doVal < thresholds.warning) {
    doStatus = 'warning';
    doBadgeColor = 'text-amber-400 bg-amber-950/70 border-amber-800';
    doStatusText = 'Advertencia';
    doIcon = <AlertTriangle className="w-4 h-4 text-amber-400" />;
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-7 gap-4">
      {/* 1. Oxígeno Disuelto (Principal Destacada) */}
      <div className={`glass-panel-glow col-span-1 sm:col-span-2 lg:col-span-2 rounded-2xl p-5 relative overflow-hidden flex flex-col justify-between border ${
        doStatus === 'critical' ? 'border-rose-500/50' : doStatus === 'warning' ? 'border-amber-500/50' : 'border-cyan-500/40'
      }`}>
        <div className="absolute top-0 right-0 w-32 h-32 bg-cyan-500/10 rounded-full blur-2xl pointer-events-none" />
        
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-cyan-300">
            <div className="p-2 rounded-xl bg-cyan-950/90 border border-cyan-800/60">
              <Droplets className="w-5 h-5 text-cyan-400" />
            </div>
            <span className="text-sm font-semibold tracking-wide uppercase text-slate-300">
              Oxígeno Disuelto (OD)
            </span>
          </div>
          <div className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold border ${doBadgeColor}`}>
            {doIcon}
            <span>{doStatusText}</span>
          </div>
        </div>

        <div className="my-3">
          <div className="flex items-baseline gap-2">
            <span className="text-4xl lg:text-5xl font-extrabold tracking-tight text-white">
              {reading ? doVal.toFixed(2) : '--'}
            </span>
            <span className="text-lg font-medium text-cyan-300/80">mg/L</span>
          </div>
          <div className="text-xs text-slate-400 mt-1 flex items-center gap-2">
            <span>Bruto: {reading?.dissolved_oxygen_raw ?? '--'}</span>
            <span>•</span>
            <span>Umbral mín: {thresholds.critical} mg/L</span>
          </div>
        </div>

        {/* Barra de Rango de Oxígeno */}
        <div className="w-full bg-slate-800/80 rounded-full h-2 overflow-hidden flex">
          <div className="h-full bg-rose-500" style={{ width: '30%' }} title="Crítico < 4.0 mg/L" />
          <div className="h-full bg-amber-500" style={{ width: '25%' }} title="Advertencia < 6.0 mg/L" />
          <div className="h-full bg-emerald-500" style={{ width: '45%' }} title="Óptimo >= 6.0 mg/L" />
        </div>
      </div>

      {/* 2. Saturación de Oxígeno */}
      <div className="glass-panel rounded-2xl p-4 flex flex-col justify-between">
        <div className="flex items-center justify-between text-slate-400">
          <span className="text-xs font-semibold uppercase tracking-wider">Saturación OD</span>
          <Percent className="w-4 h-4 text-teal-400" />
        </div>
        <div className="my-2">
          <div className="flex items-baseline gap-1.5">
            <span className="text-2xl lg:text-3xl font-bold text-white">
              {reading?.oxygen_saturation_pct ? satVal.toFixed(1) : '--'}
            </span>
            <span className="text-sm text-slate-400">%</span>
          </div>
          <p className="text-[11px] text-teal-400/80 mt-0.5">Saturación en agua</p>
        </div>
        <div className="text-[10px] text-slate-500">
          Bruto: {reading?.oxygen_saturation_raw ?? '--'}
        </div>
      </div>

      {/* 3. Temperatura del Agua */}
      <div className="glass-panel rounded-2xl p-4 flex flex-col justify-between">
        <div className="flex items-center justify-between text-slate-400">
          <span className="text-xs font-semibold uppercase tracking-wider">Temp. Agua</span>
          <Thermometer className="w-4 h-4 text-sky-400" />
        </div>
        <div className="my-2">
          <div className="flex items-baseline gap-1.5">
            <span className="text-2xl lg:text-3xl font-bold text-white">
              {reading ? tempVal.toFixed(1) : '--'}
            </span>
            <span className="text-sm text-slate-400">°C</span>
          </div>
          <p className="text-[11px] text-sky-400/80 mt-0.5">Temperatura estanque</p>
        </div>
        <div className="text-[10px] text-slate-500">
          RTC: {reading?.rtc_temperature_c ?? '--'} °C
        </div>
      </div>

      {/* 4. Voltaje de Batería Sensor */}
      <div className="glass-panel rounded-2xl p-4 flex flex-col justify-between">
        <div className="flex items-center justify-between text-slate-400">
          <span className="text-xs font-semibold uppercase tracking-wider">Batería Sensor</span>
          <BatteryCharging className="w-4 h-4 text-emerald-400" />
        </div>
        <div className="my-2">
          <div className="flex items-baseline gap-1.5">
            <span className="text-2xl lg:text-3xl font-bold text-white">
              {reading?.battery_v ? batteryV.toFixed(2) : '--'}
            </span>
            <span className="text-sm text-slate-400">V</span>
          </div>
          <p className="text-[11px] text-emerald-400/80 mt-0.5">
            {reading?.battery_mv ? `${reading.battery_mv} mV` : 'Nominal'}
          </p>
        </div>
        <div className="text-[10px] text-slate-500">
          Estado: {batteryV > 3.7 ? 'Excelente' : 'Recargar'}
        </div>
      </div>

      {/* 5. Estado del Aireador (ODrive S1 Principal) */}
      <div className="glass-panel rounded-2xl p-4 flex flex-col justify-between">
        <div className="flex items-center justify-between text-slate-400">
          <span className="text-xs font-semibold uppercase tracking-wider">Estado Aireador</span>
          <Fan className={`w-4 h-4 ${isMotorOn ? 'text-cyan-400 animate-spin' : isMotorOnline ? 'text-slate-400' : 'text-rose-500'}`} />
        </div>
        <div className="my-2">
          <span className={`inline-block px-2.5 py-1 rounded-lg text-sm font-bold tracking-wide ${
            !isMotorOnline
              ? 'bg-rose-950/60 text-rose-300 border border-rose-800/80'
              : isMotorOn
              ? 'bg-cyan-950 text-cyan-300 border border-cyan-800/80 shadow-[0_0_12px_rgba(6,182,212,0.3)]'
              : 'bg-slate-800 text-slate-400 border border-slate-700'
          }`}>
            {!isMotorOnline ? 'DESCONECTADO' : isMotorOn ? 'ENCENDIDO' : 'APAGADO'}
          </span>
          <p className="text-[11px] text-slate-400 mt-1.5">ODrive S1 (Principal)</p>
        </div>
        <div className="text-[10px] text-slate-500">
          Potencia: {motorPower} W
        </div>
      </div>

      {/* 6. Velocidad Actual Motor & RPM */}
      <div className="glass-panel rounded-2xl p-4 flex flex-col justify-between">
        <div className="flex items-center justify-between text-slate-400">
          <span className="text-xs font-semibold uppercase tracking-wider">Velocidad Motor</span>
          <Gauge className="w-4 h-4 text-blue-400" />
        </div>
        <div className="my-2">
          <div className="flex items-baseline gap-1.5">
            <span className="text-2xl lg:text-3xl font-bold text-white">
              {motorSpeed}
            </span>
            <span className="text-sm text-slate-400">%</span>
          </div>
          <p className="text-[11px] text-blue-400/80 mt-0.5">
            {isMotorOnline ? (isMotorOn ? `RPM: ${calculatedRpm}` : '0 RPM (En reposo)') : 'Sin conexión'}
          </p>
        </div>
        <div className="text-[10px] text-slate-500">
          {!isMotorOnline ? 'ODrive S1 apagado' : motor?.voltage_v ? `${motor.voltage_v}V / ${motor.current_a ?? 0}A` : 'Régimen normal'}
        </div>
      </div>
    </div>
  );
};
