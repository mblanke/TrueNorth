/**
 * ECharts styling pulled from the live CSS custom properties so charts
 * re-tint when the user switches themes. Call at option-build time.
 */
export interface TnChartColors {
  accent: string;
  accentHover: string;
  text: string;
  textMuted: string;
  border: string;
  card: string;
  warning: string;
  alert: string;
  success: string;
  palette: string[];
}

export function tnChartColors(): TnChartColors {
  const css = (name: string, fallback: string) =>
    getComputedStyle(document.body).getPropertyValue(name).trim() || fallback;

  const accent = css('--accent', '#1FB6A6');
  const accentHover = css('--accent-hover', '#3BD6C4');
  const warning = css('--warning', '#E8A838');
  const alert = css('--alert', '#D22630');
  const success = css('--success', '#1FB6A6');

  return {
    accent,
    accentHover,
    text: css('--text-primary', '#F0F4F8'),
    textMuted: css('--text-muted', '#8FA8BF'),
    border: css('--border', '#1B3A5E'),
    card: css('--bg-card', '#0E2746'),
    warning,
    alert,
    success,
    palette: [accent, '#4FC3F7', warning, alert, '#AB47BC', '#66BB6A', accentHover, '#FF8A5C'],
  };
}

/** Shared axis/grid/tooltip skeleton for cartesian charts. */
export function tnCartesianBase(c: TnChartColors): Record<string, unknown> {
  return {
    grid: { left: 8, right: 8, top: 28, bottom: 8, containLabel: true },
    textStyle: { color: c.textMuted, fontFamily: 'Inter, sans-serif' },
    tooltip: {
      trigger: 'axis',
      backgroundColor: c.card,
      borderColor: c.border,
      textStyle: { color: c.text },
    },
  };
}
