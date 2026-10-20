import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';
import { ApiService } from '@core/services/api.service';
import { RangeTopologyComponent } from './range-topology.component';

const GENERATED = {
  range_id: 'r1', generated: true, source: 'template' as const,
  notes: ['Drawn from SOC Analyst Training Environment v1.0; not a saved designer diagram.',
    'Not built yet: names, OS and addresses are what the template plans.'],
  diagram_json: { cells: [
    { id: 'z1', nodeType: 'subnet', position: { x: 0, y: 200 }, size: { width: 420, height: 150 },
      nodeData: { label: 'soc_tools', cidr: '10.60.202.0/24' } },
    { id: 'siem01', nodeType: 'server', position: { x: 20, y: 252 }, size: { width: 170, height: 64 },
      nodeData: { label: 'SIEM', os_template: 'ubuntu-2404', ip: '10.60.202.10', runtime: { status: 'planned', built: false } } },
    { id: 'fw01', nodeType: 'firewall', position: { x: 120, y: 40 }, size: { width: 170, height: 64 },
      nodeData: { label: 'Core Firewall', os_template: 'pfsense', ip: '10.60.204.1', runtime: { status: 'planned', built: false } } },
    { type: 'standard.Link', id: 'l', source: { id: 'fw01' }, target: { id: 'z1' }, linkData: { ip: '10.60.202.1' } },
  ] },
};

describe('RangeTopologyComponent', () => {
  let fixture: ComponentFixture<RangeTopologyComponent>;
  let el: HTMLElement;
  let api: jasmine.SpyObj<ApiService>;

  async function render(response: unknown) {
    api = jasmine.createSpyObj('ApiService', ['getRangeDiagram']);
    api.getRangeDiagram.and.returnValue(response as any);
    await TestBed.configureTestingModule({
      imports: [RangeTopologyComponent, NoopAnimationsModule],
      providers: [{ provide: ApiService, useValue: api }],
    }).compileComponents();
    fixture = TestBed.createComponent(RangeTopologyComponent);
    fixture.componentRef.setInput('rangeId', 'r1');
    fixture.detectChanges();
    el = fixture.nativeElement;
  }

  it('draws a generated topology and says where it came from', async () => {
    await render(of(GENERATED));
    expect(api.getRangeDiagram).toHaveBeenCalledOnceWith('r1');
    expect(el.textContent).toContain('Drawn from the template');
    expect(el.textContent).toContain('SOC Analyst Training Environment');
    expect(el.textContent).toContain('1 networks · 2 hosts');
    expect(el.querySelectorAll('svg.topo-svg line.link').length).toBe(1);
    expect(el.textContent).toContain('10.60.202.1'); // the firewall's address on that network
    expect(el.textContent).toContain('planned');
    // The same picture in words, for screen readers.
    const sr = el.querySelector('ul.sr-only')!.textContent!;
    expect(sr).toContain('soc_tools');
    expect(sr).toContain('SIEM');
    expect(sr).toContain('Between networks');
    expect(sr).toContain('Core Firewall');
  });

  it('shows a saved designer diagram as such, without the template notes', async () => {
    await render(of({ ...GENERATED, generated: false, source: 'saved', notes: [] }));
    expect(el.textContent).toContain('Saved designer diagram');
    expect(el.querySelector('ul.notes')).toBeNull();
  });

  it('says when there is nothing to draw', async () => {
    await render(of({ range_id: 'r1', generated: false, source: 'none', diagram_json: { cells: [] },
      notes: ["The range's template declares no hosts, so there is nothing to draw."] }));
    expect(el.textContent).toContain('declares no hosts');
    expect(el.querySelector('svg.topo-svg')).toBeNull();
  });

  it('reports a failed read as an error with a retry, not as an empty picture', async () => {
    await render(throwError(() => new HttpErrorResponse({ status: 500, error: { detail: 'database unavailable' } })));
    const alert = el.querySelector('[role="alert"]')!;
    expect(alert.textContent).toContain('database unavailable');
    expect(el.textContent).not.toContain('nothing to draw');
    api.getRangeDiagram.and.returnValue(of(GENERATED));
    (alert.querySelector('button') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(el.textContent).toContain('Drawn from the template');
  });

  it('switches between fit and full size', async () => {
    await render(of(GENERATED));
    const svg = () => el.querySelector('svg.topo-svg') as SVGElement;
    expect(svg().style.width).toBe('100%');
    const toggle = Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Full size'))!;
    toggle.click();
    fixture.detectChanges();
    expect(svg().style.width).not.toBe('100%');
    expect(toggle.getAttribute('aria-pressed')).toBe('true');
  });
});
