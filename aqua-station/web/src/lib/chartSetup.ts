import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  Filler,
} from 'chart.js';
import annotationPlugin from 'chartjs-plugin-annotation';

// Canvas background plugin: ensures PNG exports and transparent canvases have a solid dark SCADA background
const canvasBackgroundPlugin = {
  id: 'canvasBackground',
  beforeDraw: (chart: any) => {
    const { ctx } = chart;
    ctx.save();
    ctx.globalCompositeOperation = 'destination-over';
    ctx.fillStyle = '#0b0f19'; // SCADA panel well background
    ctx.fillRect(0, 0, chart.width, chart.height);
    ctx.restore();
  },
};

// Register core Chart.js components, background plugin, and annotation plugin
ChartJS.register(
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  Filler,
  annotationPlugin,
  canvasBackgroundPlugin
);

// chartjs-plugin-zoom relies on Hammer.js which accesses `window` at require-time.
// Guard with `typeof window !== 'undefined'` to avoid SSR / Next.js static prerender errors.
if (typeof window !== 'undefined') {
  try {
    const zoomPlugin = require('chartjs-plugin-zoom');
    ChartJS.register(zoomPlugin.default || zoomPlugin);
  } catch (err) {
    console.warn('Failed to register chartjs-plugin-zoom:', err);
  }
}

// Apply SCADA theme defaults
ChartJS.defaults.color = '#94a3b8';
ChartJS.defaults.font.family = 'var(--font-mono), ui-monospace, SFMono-Regular, Menlo, monospace';
ChartJS.defaults.font.size = 10;
ChartJS.defaults.borderColor = '#1c2436';

export default ChartJS;
