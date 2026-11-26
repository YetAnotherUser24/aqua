#!/usr/bin/env python3
import re
from pathlib import Path

template_path = Path('/root/aqua/aqua-station/tools/portal_template.html')
text = template_path.read_text(encoding='utf-8')

# 1. Update nav tab badge for experiments
text = re.sub(
    r'(<button class="nav-tab" id="tab-btn-exps"[^>]*>.*?<span class="tab-badge">)\d+(</span>)',
    r'\g<1><span id="exp-badge-count">0</span>\g<2>',
    text,
    flags=re.DOTALL
)

# 2. Empty static options in <select id="exp-select">
text = re.sub(
    r'(<select id="exp-select"[^>]*>).*?(</select>)',
    r'\g<1>\n            <option value="">Cargando ensayos...</option>\n          \g<2>',
    text,
    flags=re.DOTALL
)

# 3. Empty static cards in <div class="exp-grid" id="exp-grid-container">
text = re.sub(
    r'(<div class="exp-grid" id="exp-grid-container">).*?(</div>\s*</div>\s*<!-- TAB 3: DAILY RAW TELEMETRY -->)',
    r'\g<1>\n      </div>\n    </div>\n\n    <!-- TAB 3: DAILY RAW TELEMETRY -->',
    text,
    flags=re.DOTALL
)

# 4. In script, update render logic & DOMContentLoaded
old_init = """    // Initialize application on DOM ready
    document.addEventListener('DOMContentLoaded', () => {
      const defaultId = "EXP-2026-10-28-01";
      if (defaultId) {
        currentExpId = defaultId;
        loadExperiment(defaultId, 'native');
      }
    });"""

new_init = """    // Render UI dynamically from EXPERIMENTS array
    function renderExperimentUI() {
      if (!EXPERIMENTS || EXPERIMENTS.length === 0) return;

      // Update badge
      const badge = document.getElementById('exp-badge-count');
      if (badge) badge.textContent = EXPERIMENTS.length;

      // Populate Select Dropdown
      const select = document.getElementById('exp-select');
      if (select) {
        select.innerHTML = EXPERIMENTS.map(e => {
          let badgeStr = '';
          if (e.native_csv && e.wide_csv) badgeStr = ' [⚡ Nativo + ⏱ 1 Hz]';
          else if (e.native_csv) badgeStr = ' [⚡ Solo Nativo]';
          else if (e.wide_csv) badgeStr = ' [⏱ Solo 1 Hz]';
          const titleStr = e.title ? (' — ' + e.title) : '';
          return '<option value="' + e.id + '">' + e.id + titleStr + badgeStr + '</option>';
        }).join('');
      }

      // Populate Experiment Cards Grid
      const grid = document.getElementById('exp-grid-container');
      if (grid) {
        grid.innerHTML = EXPERIMENTS.map(e => {
          const title = e.title || 'Ensayo de Oxigenación';
          const roles = (e.roles || ['od_sensor', 'mixer', 'aerator', 'pump']).map(r => '<span class="role-badge">' + r + '</span>').join('');
          
          let badgesHeader = '';
          if (e.native_csv) {
            badgesHeader += '<span class="role-badge" style="background: rgba(168, 85, 247, 0.15); color: #c084fc; border-color: rgba(168, 85, 247, 0.4);">⚡ Nativo (Tesis)</span> ';
          }
          if (e.wide_csv) {
            badgesHeader += '<span class="role-badge" style="background: rgba(56, 189, 248, 0.15); color: #38bdf8; border-color: rgba(56, 189, 248, 0.4);">⏱ 1 Hz</span>';
          }

          let actionButtons = '';
          if (e.native_csv) {
            actionButtons += '<a href="/' + e.native_csv + '" class="btn-action btn-highlight" style="flex: 1; justify-content: center;" download title="Descargar CSV nativo a alta frecuencia (5-20 Hz) para tesis"><span>⚡ CSV Nativo (Tesis)</span></a>';
          }
          if (e.wide_csv) {
            actionButtons += '<a href="/' + e.wide_csv + '" class="btn-action" style="justify-content: center;" download title="Descargar CSV wide sincronizado a 1 Hz"><span>📄 CSV 1 Hz</span></a>';
          }
          if (e.native_bundle) {
            actionButtons += '<a href="/' + e.native_bundle + '" class="btn-action" style="justify-content: center;" download title="Descargar paquete completo comprimido (.tar.gz)"><span>📦 .tar.gz</span></a>';
          } else if (e.wide_bundle) {
            actionButtons += '<a href="/' + e.wide_bundle + '" class="btn-action" style="justify-content: center;" download title="Descargar paquete completo comprimido (.tar.gz)"><span>📦 .tar.gz</span></a>';
          }

          return '<div class="exp-card" data-id="' + e.id + '" data-title="' + title.toLowerCase() + '">' +
            '<div>' +
              '<div class="exp-card-header">' +
                '<span class="exp-card-title">' + e.id + '</span>' +
                '<div>' + badgesHeader + '</div>' +
              '</div>' +
              '<div class="exp-card-subtitle">' + title + '</div>' +
              '<div class="role-badges">' + roles + '</div>' +
            '</div>' +
            '<div class="exp-card-actions">' +
              '<button class="btn-action btn-primary" onclick="selectAndGraph(\'' + e.id + '\')">' +
                '<span>📈 Cargar en Visualizador</span>' +
              '</button>' +
              '<div style="display: flex; gap: 0.4rem; flex-wrap: wrap;">' +
                actionButtons +
              '</div>' +
            '</div>' +
          '</div>';
        }).join('');
      }
    }

    // Initialize application on DOM ready
    document.addEventListener('DOMContentLoaded', () => {
      renderExperimentUI();
      const defaultId = (EXPERIMENTS && EXPERIMENTS.length > 0) ? EXPERIMENTS[0].id : null;
      if (defaultId) {
        currentExpId = defaultId;
        const select = document.getElementById('exp-select');
        if (select) select.value = defaultId;
        loadExperiment(defaultId, 'native');
      }
    });"""

if old_init in text:
    text = text.replace(old_init, new_init)
    template_path.write_text(text, encoding='utf-8')
    print('SUCCESS: Updated portal_template.html successfully!')
else:
    print('ERROR: old_init not found in template!')
