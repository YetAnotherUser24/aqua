import type { Metadata } from 'next';
import { Inter, JetBrains_Mono } from 'next/font/google';
import './globals.css';

// The mockup's two faces, loaded through next/font so they are self-hosted and do not flash.
// Inter for the interface, JetBrains Mono for every number — the mono face is what makes the
// telemetry columns line up.
const inter = Inter({
  subsets: ['latin'],
  weight: ['400', '500', '600', '700', '800'],
  variable: '--font-sans',
  display: 'swap',
});
const jetbrainsMono = JetBrains_Mono({
  subsets: ['latin'],
  weight: ['400', '500', '600', '700'],
  variable: '--font-mono',
  display: 'swap',
});

export const metadata: Metadata = {
  title: 'AquaSCADA Lab — Consola de Investigación & Control de Oxigenación IoT',
  description:
    'Banco de acuicultura de recirculación: telemetría de oxígeno disuelto, control de aireación y datos de ensayo para el despliegue aqua-station.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="es" className={`dark ${inter.variable} ${jetbrainsMono.variable}`}>
      <body className="antialiased min-h-screen text-slate-100 selection:bg-sky-500/30 selection:text-sky-200">
        {children}
      </body>
    </html>
  );
}
