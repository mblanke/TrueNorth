import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { environment } from '@env/environment';
import { RunDetail } from '@core/arc2/studio';
import { AuthService } from '@core/services/auth.service';
import { CourseStudioComponent } from './course-studio.component';

const API = `${environment.apiUrl}/arc2`;

function detail(over: Partial<RunDetail> = {}): RunDetail {
  return {
    slug: 'arc2-wireshark', name: 'Wireshark basics', title: null, code: null, request: '60 min beginner course',
    phase: 'outline', phase_text: 'Outline ready for your review',
    stages: [{ key: 'content-architect', name: 'Content Architect', state: 'done' }, { key: 'code-generator', name: 'Code Generator', state: 'pending' }],
    gates: { outline: { state: 'pending' }, preview: { state: 'n/a' } },
    qa: { result: null, cycle: 0, rework_stage: null, checked_at: null },
    actions_open: 0, actions_blocking: 0, job: null, updated_at: null,
    messages: [{ role: 'user', text: '60 min beginner course', ts: null }, { role: 'pipeline', text: 'Outline ready.', ts: null }],
    outline: { modules: [{ id: 'mod_001', title: 'Capture basics', minutes: 30, objective_ids: ['M01-O01'] }] },
    objectives: [{ id: 'M01-O01', text: 'Capture traffic on an interface' }],
    modules: [], pages: [], lab: { range: null, injects: [], noise_floor: [] },
    findings: [], human_actions: [], files: [], package_ready: false, ...over,
  };
}

describe('CourseStudioComponent', () => {
  let fixture: ComponentFixture<CourseStudioComponent>;
  let http: HttpTestingController;
  let el: HTMLElement;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [CourseStudioComponent],
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([]),
        { provide: AuthService, useValue: { logout: () => undefined } }],
    }).compileComponents();
    fixture = TestBed.createComponent(CourseStudioComponent);
    http = TestBed.inject(HttpTestingController);
    el = fixture.nativeElement;
  });

  /** Stop polling (the page clears its timer on destroy) and drain what is left. */
  function finish(): void {
    fixture.destroy();
    tick(0);
    http.match(() => true).forEach(r => r.flush({}));
  }

  it('Send on a new project starts the course with the name and the description', fakeAsync(() => {
    fixture.detectChanges();
    http.expectOne(`${API}/runs`).flush({ runs: [], runner_seen: null });
    fixture.detectChanges();

    const name = el.querySelector<HTMLInputElement>('.newp input')!;
    name.value = 'Wireshark basics';
    el.querySelector<HTMLFormElement>('.newp')!.dispatchEvent(new Event('submit'));
    fixture.detectChanges();
    tick();
    expect(el.querySelector('.proj[aria-current="true"] strong')!.textContent).toContain('Wireshark basics');

    const box = el.querySelector<HTMLInputElement>('.send input')!;
    expect(box.disabled).toBeFalse();
    box.value = '60 min beginner course on packet sniffing and Wireshark';
    el.querySelector<HTMLFormElement>('.send')!.dispatchEvent(new Event('submit'));

    const req = http.expectOne(r => r.method === 'POST' && r.url === `${API}/runs`);
    expect(req.request.body).toEqual({ name: 'Wireshark basics', request: '60 min beginner course on packet sniffing and Wireshark' });
    req.flush(detail({ phase: 'queued', phase_text: 'Waiting for the runner', messages: [{ role: 'user', text: '60 min…', ts: null }] }));
    fixture.detectChanges();
    http.match(`${API}/runs`).forEach(r => r.flush({ runs: [], runner_seen: null }));
    http.match(`${API}/runs/arc2-wireshark`).forEach(r => r.flush(detail({ phase: 'queued', phase_text: 'Waiting for the runner' })));
    fixture.detectChanges();
    expect(el.querySelector('.btn-approve')!.textContent).toContain('Working');
    finish();
  }));

  it('shows the outline and Accept Outline resumes the run', fakeAsync(() => {
    fixture.detectChanges();
    http.expectOne(`${API}/runs`).flush({ runs: [detail()], runner_seen: null });
    http.expectOne(`${API}/runs/arc2-wireshark`).flush(detail());
    fixture.detectChanges();
    tick();
    expect(el.querySelector('.tabs button[aria-pressed="true"]')!.textContent).toContain('Outline');
    expect(el.textContent).toContain('Capture basics');
    const btn = el.querySelector<HTMLButtonElement>('.btn-approve')!;
    expect(btn.textContent).toContain('Accept Outline');
    btn.click();
    const req = http.expectOne(`${API}/runs/arc2-wireshark/reply`);
    expect(req.request.body).toEqual({ action: 'accept', text: undefined });
    req.flush(detail({ phase: 'queued', phase_text: 'Waiting for the runner' }));
    finish();
  }));

  it('shows the engine’s refusal instead of failing silently', fakeAsync(() => {
    fixture.detectChanges();
    http.expectOne(`${API}/runs`).flush({ runs: [detail()], runner_seen: null });
    http.expectOne(`${API}/runs/arc2-wireshark`).flush(detail());
    fixture.detectChanges();
    tick();
    el.querySelector<HTMLButtonElement>('.btn-approve')!.click();
    http.expectOne(`${API}/runs/arc2-wireshark/reply`).flush(
      { detail: 'ARC² is still working on this run. Wait for it to stop at a review.' }, { status: 409, statusText: 'Conflict' });
    fixture.detectChanges();
    expect(el.textContent).toContain('still working on this run');
    finish();
  }));
});
