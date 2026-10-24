import React, { useState } from 'react';
import { AlertTriangle, Power, ZapOff } from 'lucide-react';

interface ManualOverridePanelProps {
  onForceCommand: (target: string, action: string) => void;
}

export const ManualOverridePanel: React.FC<ManualOverridePanelProps> = ({ onForceCommand }) => {
  const [isGodMode, setIsGodMode] = useState(false);

  return (
    <div className="p-4 rounded-xl border border-rose-900/50 bg-rose-950/20 shadow-lg">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2 text-rose-400">
          <AlertTriangle className="w-5 h-5" />
          <h3 className="font-bold tracking-wide">Manual Override (Modo Dios)</h3>
        </div>
        <button
          onClick={() => setIsGodMode(!isGodMode)}
          className={`px-4 py-1.5 rounded-lg text-xs font-bold transition-all ${
            isGodMode 
              ? 'bg-rose-600 hover:bg-rose-500 text-white shadow-md shadow-rose-900/50' 
              : 'bg-slate-800 hover:bg-slate-700 text-slate-400'
          }`}
        >
          {isGodMode ? 'Desactivar Modo Seguro' : 'Habilitar Override'}
        </button>
      </div>
      
      {!isGodMode && (
         <p className="text-[11px] text-slate-500">
           Controles de emergencia y purgado de tuberías. Evade los enclavamientos lógicos.
         </p>
      )}

      {isGodMode && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mt-4 animate-in fade-in zoom-in-95 duration-200">
          <div className="p-3 bg-slate-900/80 rounded-xl border border-slate-800 flex flex-col justify-between">
            <div>
               <h4 className="text-xs font-bold text-slate-300 mb-1">Planta 1 (Bomba)</h4>
               <p className="text-[10px] text-slate-500 mb-3">Ideal para purgar químico antes de ensayo.</p>
            </div>
            <div className="flex gap-2">
              <button onClick={() => onForceCommand('pump', 'force_on')} className="flex-1 py-2 bg-emerald-950 hover:bg-emerald-900 text-emerald-400 border border-emerald-800/50 rounded-lg text-xs font-bold flex items-center justify-center gap-1 transition-colors"><Power className="w-3.5 h-3.5"/> ON</button>
              <button onClick={() => onForceCommand('pump', 'force_off')} className="flex-1 py-2 bg-rose-950 hover:bg-rose-900 text-rose-400 border border-rose-800/50 rounded-lg text-xs font-bold flex items-center justify-center gap-1 transition-colors"><ZapOff className="w-3.5 h-3.5"/> OFF</button>
            </div>
          </div>
          <div className="p-3 bg-slate-900/80 rounded-xl border border-slate-800 flex flex-col justify-between">
            <div>
               <h4 className="text-xs font-bold text-slate-300 mb-1">Planta 2 (ODrive)</h4>
               <p className="text-[10px] text-slate-500 mb-3">Bypass directo a 200 RPM en lazo abierto.</p>
            </div>
            <div className="flex gap-2">
              <button onClick={() => onForceCommand('odrive', 'force_on')} className="flex-1 py-2 bg-emerald-950 hover:bg-emerald-900 text-emerald-400 border border-emerald-800/50 rounded-lg text-xs font-bold flex items-center justify-center gap-1 transition-colors"><Power className="w-3.5 h-3.5"/> ON</button>
              <button onClick={() => onForceCommand('odrive', 'force_off')} className="flex-1 py-2 bg-rose-950 hover:bg-rose-900 text-rose-400 border border-rose-800/50 rounded-lg text-xs font-bold flex items-center justify-center gap-1 transition-colors"><ZapOff className="w-3.5 h-3.5"/> OFF</button>
            </div>
          </div>
          <div className="p-3 bg-slate-900/80 rounded-xl border border-slate-800 flex flex-col justify-between">
            <div>
               <h4 className="text-xs font-bold text-slate-300 mb-1">Auxiliar (T-200)</h4>
               <p className="text-[10px] text-slate-500 mb-3">Arranque inmediato a 1600µs.</p>
            </div>
            <div className="flex gap-2">
              <button onClick={() => onForceCommand('mixer', 'force_on')} className="flex-1 py-2 bg-emerald-950 hover:bg-emerald-900 text-emerald-400 border border-emerald-800/50 rounded-lg text-xs font-bold flex items-center justify-center gap-1 transition-colors"><Power className="w-3.5 h-3.5"/> ON</button>
              <button onClick={() => onForceCommand('mixer', 'force_off')} className="flex-1 py-2 bg-rose-950 hover:bg-rose-900 text-rose-400 border border-rose-800/50 rounded-lg text-xs font-bold flex items-center justify-center gap-1 transition-colors"><ZapOff className="w-3.5 h-3.5"/> OFF</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
