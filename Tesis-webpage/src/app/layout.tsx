import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'AquaControl - Monitoreo y Control de Acuicultura',
  description: 'Plataforma profesional de telemetría IoT de Oxígeno Disuelto y control de aireación con Thruster Blue Robotics T200.',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="es" className="dark">
      <body className="antialiased min-h-screen text-slate-100 selection:bg-cyan-500 selection:text-black">
        {children}
      </body>
    </html>
  );
}
