'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { Header } from '@/components/Header';
import { MetricCards } from '@/components/MetricCards';
import { SensorControlPanel } from '@/components/SensorControlPanel';
import { ClearDatabaseModal } from '@/components/ClearDatabaseModal';
import { MotorControlPanel } from '@/components/MotorControlPanel';
import { MixerControlPanel } from '@/components/MixerControlPanel';
import { ManualOverridePanel } from '@/components/ManualOverridePanel';
import { OrchestratorBar } from '@/components/OrchestratorBar';
import { ChartsSection } from '@/components/ChartsSection';
import { EventsTable } from '@/components/EventsTable';
import { AlertsPanel } from '@/components/AlertsPanel';
import {
  DashboardSummaryResponse,
  HistoryDataPoint,
  MotorEvent,
  Alert,
  Experiment,
  OrchestratorState
} from '@/types';
import {
  demoThresholds,
  demoSensorDevice,
  demoMotorDevice,
  demoEscDevice,
  getDemoLatestSensorReading,
  getDemoLatestMotorTelemetry,
  getDemoLatestEscTelemetry,
  generateDemoHistory,
  demoEvents,
  demoAlerts
} from '@/lib/demoData';
import { ShieldCheck, Info, Sparkles, Sliders } from 'lucide-react';
import { formatPeruTime } from '@/lib/dateUtils';

export default function DashboardPage() {
  // Estado principal consolidado
  const [summary, setSummary] = useState<DashboardSummaryResponse>({
    sensorDevice: null,
    motorDevice: null,
    escDevice: null,
    latestSensorReading: null,
    latestMotorTelemetry: null,
    latestEscTelemetry: null,
    thresholds: demoThresholds,
    activeAlertsCount: 0,
    systemHealth: 'optimal',
    isLive: false
  });

  const [history, setHistory] = useState<HistoryDataPoint[]>([]);
  const [events, setEvents] = useState<MotorEvent[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [selectedRange, setSelectedRange] = useState<string>('24h');
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [lastUpdated, setLastUpdated] = useState<string>('Recién cargado');
  const [activeExperiment, setActiveExperiment] = useState<Experiment | null>(null);
  const [showClearDbModal, setShowClearDbModal] = useState<boolean>(false);

  // Función principal para cargar datos de la dashboard
  const fetchDashboardData = useCallback(async (isBackground: boolean = false) => {
    if (!isBackground) setIsLoading(true);

    try {
      // 1. Obtener Resumen General (con cache-buster para evitar respuestas cacheadas por CDN o navegador)
      const summaryRes = await fetch(`/api/dashboard/summary?_t=${Date.now()}`, { cache: 'no-store' });
      if (summaryRes.ok) {
        const summaryData: DashboardSummaryResponse = await summaryRes.json();
        setSummary(summaryData);
        // Sincronizar reactivamente el experimento activo con el backend canónico
        setActiveExperiment(summaryData.activeExperiment || null);
      }

      // 2. Obtener Historial de Gráficas
      const historyRes = await fetch(`/api/dashboard/history?range=${selectedRange}`, { cache: 'no-store' });
      if (historyRes.ok) {
        const historyData = await historyRes.json();
        setHistory(historyData.data || []);
      }

      // 3. Obtener Eventos
      const eventsRes = await fetch('/api/events', { cache: 'no-store' });
      if (eventsRes.ok) {
        const eventsData = await eventsRes.json();
        setEvents(eventsData.events || []);
      }

      // 4. Obtener Alertas
      const alertsRes = await fetch('/api/alerts', { cache: 'no-store' });
      if (alertsRes.ok) {
        const alertsData = await alertsRes.json();
        setAlerts(alertsData.alerts || []);
      }

      setLastUpdated(`Actualizado: ${formatPeruTime(new Date(), true)} (GMT-5)`);
    } catch (err) {
      console.error('Error refrescando datos de dashboard:', err);
      // Si ocurre un error de red local, mantener datos demo
      if (history.length === 0) {
        setHistory(generateDemoHistory(24));
      }
    } finally {
      if (!isBackground) setIsLoading(false);
    }
  }, [selectedRange, history.length]);

  // Carga inicial y actualización periódica (acelerada a 4s durante experimento activo, 15s en reposo)
  useEffect(() => {
    fetchDashboardData(false);

    const pollIntervalMs = activeExperiment ? 4000 : 15000;
    const interval = setInterval(() => {
      fetchDashboardData(true);
    }, pollIntervalMs);

    return () => clearInterval(interval);
  }, [fetchDashboardData, activeExperiment]);

  // Acción para resolver o reconocer alertas
  const handleAcknowledgeAlert = async (alertId: number, newStatus: 'reconocida' | 'resuelta') => {
    try {
      await fetch('/api/alerts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ alert_id: alertId, action: newStatus })
      });
      // Actualizar estado local inmediatamente
      setAlerts(prev =>
        prev.map(a => (a.id === alertId ? { ...a, status: newStatus } : a))
      );
    } catch (err) {
      console.error('Error actualizando alerta:', err);
    }
  };

  const handleForceCommand = async (target: string, action: string) => {
     let device_id = '';
     if (target === 'odrive') device_id = 'b0000000-0000-0000-0000-000000000002';
     if (target === 'mixer') device_id = 'c0000000-0000-0000-0000-000000000003';
     if (target === 'pump') device_id = 'd0000000-0000-0000-0000-000000000004';

     try {
       await fetch('/api/commands', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            device_id,
            command_type: 'set_config',
            payload: { action },
            requested_by: 'Manual Override (God Mode)'
          })
       });
       // V4: bitácora de seguridad de la anulación manual (no bloqueante).
       fetch('/api/events/override', {
         method: 'POST',
         headers: { 'Content-Type': 'application/json' },
         body: JSON.stringify({
           action,
           target,
           device_id,
           requested_by: 'Manual Override (God Mode)',
           experiment_id: activeExperiment?.id || null
         })
       }).catch(() => {});
       fetchDashboardData(true);
     } catch (err) {
       console.error('Error enviando force command:', err);
     }
  };

  /** V4: transiciona la máquina de estados global del orquestador. */
  const handleChangeSystemState = async (state: OrchestratorState) => {
    try {
      await fetch('/api/system/state', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          state,
          experiment_id: state === 'ACTIVE_EXPERIMENT' ? (activeExperiment?.id ?? null) : null,
          requested_by: 'Operador Web (Orquestador)'
        })
      });
      await fetchDashboardData(true);
    } catch (err) {
      console.error('Error cambiando estado del orquestador:', err);
    }
  };

  return (
    <div className="min-h-screen bg-[#080c14] text-slate-100 flex flex-col">
      {/* Header Superior */}
      <Header
        sensorDevice={summary.sensorDevice}
        motorDevice={summary.motorDevice}
        escDevice={summary.escDevice}
        isLive={summary.isLive}
        isLoading={isLoading}
        onRefresh={() => fetchDashboardData(false)}
        lastUpdatedText={lastUpdated}
        onOpenClearModal={() => setShowClearDbModal(true)}
      />

      {/* Contenedor Principal */}
      <main className="flex-1 p-4 lg:p-8 max-w-[1680px] w-full mx-auto flex flex-col gap-6">
        
        {/* Banner de Bienvenida y Estado del Estanque */}
        <div className="flex flex-wrap items-center justify-between gap-4 bg-slate-900/60 border border-cyan-900/30 p-4 rounded-2xl">
          <div className="flex items-center gap-3">
            <div className="p-2.5 rounded-xl bg-cyan-950 text-cyan-400 border border-cyan-800/80 shadow-md">
              <Sparkles className="w-5 h-5" />
            </div>
            <div>
              <h2 className="text-base font-bold text-white">
                Monitoreo y Control en Tiempo Real - Estanque Principal
              </h2>
              <p className="text-xs text-slate-400">
                Lazo cerrado con sensor óptico de OD, actuador principal ODrive S1 (M8325s) y propulsor auxiliar T-200 (ESC PWM)
              </p>
            </div>
          </div>
          <div className="flex items-center gap-3 text-xs">
            <span className="text-slate-400">Intervalo de telemetría: <strong className="text-cyan-400 font-semibold">15s</strong></span>
            <span className="text-slate-600">|</span>
            <span className="text-slate-400">Ubicación: <strong className="text-slate-200">Zona de Cultivo Norte</strong></span>
          </div>
        </div>

        {/* Barra de Orquestación Global (IDLE / ACTIVE_EXPERIMENT / MANUAL_OVERRIDE) */}
        <section>
          <OrchestratorBar
            systemState={summary.systemState || null}
            onChangeState={handleChangeSystemState}
          />
        </section>

        {/* 1. Tarjetas de Resumen KPI y Semáforo de Oxígeno Disuelto */}
        <section>
          <MetricCards
            reading={summary.latestSensorReading}
            motor={summary.latestMotorTelemetry}
            thresholds={summary.thresholds}
            lastUpdated={lastUpdated}
            motorDevice={summary.motorDevice}
            sensorDevice={summary.sensorDevice}
          />
        </section>

        {/* 2. Panel de Control y Configuración del Sensor de Oxígeno Disuelto (OD-Logger) */}
        <section>
          <SensorControlPanel
            sensorDevice={summary.sensorDevice}
            activeExperiment={activeExperiment}
            onCommandSent={() => fetchDashboardData(true)}
            onExperimentStarted={(exp) => {
              setActiveExperiment(exp);
              const el = document.getElementById('charts-section');
              if (el) {
                el.scrollIntoView({ behavior: 'smooth' });
              }
            }}
            onExperimentStopped={() => {
              setActiveExperiment(null);
            }}
          />
        </section>

        {/* 3. Paneles de Control de Aireación (Dual: ODrive S1 Principal & T-200 ESC Auxiliar) */}
        <section className="grid grid-cols-1 xl:grid-cols-3 gap-6 items-stretch">
          <div className="xl:col-span-2">
            <MotorControlPanel
              motorDevice={summary.motorDevice}
              currentTelemetry={summary.latestMotorTelemetry}
              onCommandSent={() => fetchDashboardData(true)}
            />
          </div>
          <div className="xl:col-span-1">
            <MixerControlPanel
              mixerDevice={summary.escDevice || null}
              currentTelemetry={summary.latestEscTelemetry || null}
              onCommandSent={() => fetchDashboardData(true)}
            />
          </div>
        </section>

        {/* 4. Panel de Emergencia / Manual Override */}
        <section>
          <ManualOverridePanel onForceCommand={handleForceCommand} />
        </section>

        {/* 5. Gráficas Principales (OD, Velocidad, Combinada y Experimento Activo) */}
        <section id="charts-section">
          <ChartsSection
            history={history}
            thresholds={summary.thresholds}
            selectedRange={selectedRange}
            onRangeChange={(range) => setSelectedRange(range)}
            isLoading={isLoading}
            activeExperiment={activeExperiment}
          />
        </section>

        {/* 4. Tablas de Eventos y Panel de Alertas */}
        <section className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <EventsTable events={events} />
          <AlertsPanel alerts={alerts} onAcknowledgeAlert={handleAcknowledgeAlert} />
        </section>
      </main>

      {/* Footer */}
      <footer className="mt-auto border-t border-slate-800/80 py-4 px-6 text-center text-xs text-slate-500 flex flex-wrap items-center justify-between gap-4">
        <span>AquaControl IoT © 2026 - Sistema de Telemetría y Oxigenación de Acuicultura</span>
        <div className="flex items-center gap-4 text-[11px]">
          <span>Next.js 14 App Router</span>
          <span>•</span>
          <span>Supabase PostgreSQL</span>
          <span>•</span>
          <span>Vercel Edge Ready</span>
        </div>
      </footer>

      {/* Modal de Limpieza Granular de Base de Datos */}
      <ClearDatabaseModal
        isOpen={showClearDbModal}
        onClose={() => setShowClearDbModal(false)}
        onSuccess={() => fetchDashboardData(false)}
      />
    </div>
  );
}
