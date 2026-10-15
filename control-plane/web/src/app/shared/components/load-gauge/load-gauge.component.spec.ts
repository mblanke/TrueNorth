import { TestBed } from '@angular/core/testing';
import { LoadGaugeComponent, loadBand, loadPct } from './load-gauge.component';

describe('LoadGaugeComponent (semicircle load gauge)', () => {
  function render(label: string, value: number | null | undefined): HTMLElement {
    TestBed.configureTestingModule({ imports: [LoadGaugeComponent] });
    const fixture = TestBed.createComponent(LoadGaugeComponent);
    fixture.componentRef.setInput('label', label);
    fixture.componentRef.setInput('value', value);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('bands 50 as normal, 80 as warning and 95 as critical, with the theme colour per band', () => {
    const strokes = new Set<string>();
    for (const [v, band, token] of [[50, 'normal', '--success'], [80, 'warning', '--warning'], [95, 'critical', '--alert']] as const) {
      TestBed.resetTestingModule();
      const el = render('CPU', v);
      expect(el.classList).toContain('band-' + band);
      const fill = el.querySelector('path.fill') as SVGPathElement;
      expect(fill).not.toBeNull();
      expect(fill.getAttribute('stroke-dasharray')).toBe(`${v} 100`);
      const probe = document.createElement('span');
      probe.style.color = `var(${token})`;
      el.appendChild(probe);
      expect(getComputedStyle(fill).stroke).toBe(getComputedStyle(probe).color);
      strokes.add(getComputedStyle(fill).stroke);
    }
    expect(strokes.size).toBe(3);  // three distinct colours, not one fallback
  });

  it('bands on the shown figure: 74.6 reads "75%" and is warning', () => {
    expect(render('Memory', 74.6).classList).toContain('band-warning');
  });

  it('shows the value and caption', () => {
    const el = render('Storage', 42.4);
    expect(el.querySelector('text.value')?.textContent?.trim()).toBe('42%');
    expect(el.querySelector('.caption')?.textContent?.trim()).toBe('Storage');
  });

  it('exposes role="meter" with value, min, max and a spoken label', () => {
    const el = render('CPU', 42);
    expect(el.getAttribute('role')).toBe('meter');
    expect(el.getAttribute('aria-valuenow')).toBe('42');
    expect(el.getAttribute('aria-valuemin')).toBe('0');
    expect(el.getAttribute('aria-valuemax')).toBe('100');
    expect(el.getAttribute('aria-label')).toBe('CPU load 42 percent');
  });

  for (const missing of [null, undefined]) {
    it(`renders ${missing} as a grey empty gauge labelled "not reported", never 0%`, () => {
      const el = render('CPU', missing);
      expect(el.classList).toContain('band-unknown');
      expect(el.querySelector('path.fill')).toBeNull();
      expect(el.textContent).toContain('not reported');
      expect(el.textContent).not.toContain('0%');
      expect(el.getAttribute('aria-valuenow')).toBeNull();
      expect(el.getAttribute('aria-label')).toBe('CPU load not reported');
    });
  }

  it('helpers: loadPct is null (not 0) when a figure is missing or the total is zero', () => {
    expect(loadPct(null, 100)).toBeNull();
    expect(loadPct(10, null)).toBeNull();
    expect(loadPct(10, 0)).toBeNull();
    expect(loadPct(25, 100)).toBe(25);
    expect(loadBand(undefined)).toBe('unknown');
    expect(loadBand(90)).toBe('critical');
  });
});
