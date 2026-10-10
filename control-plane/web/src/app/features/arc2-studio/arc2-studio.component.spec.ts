import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { environment } from '@env/environment';
import { RunDetail } from '@core/arc2/studio';
import { signal } from '@angular/core';
import { AuthService } from '@core/services/auth.service';
import { Arc2StudioComponent } from './arc2-studio.component';

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

describe('Arc2StudioComponent', () => {
  let fixture: ComponentFixture<Arc2StudioComponent>;
  let http: HttpTestingController;
  let el: HTMLElement;
  const isAdmin = signal(false);

  beforeEach(async () => {
    isAdmin.set(false);
    await TestBed.configureTestingModule({
      imports: [Arc2StudioComponent],
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([]),
        { provide: AuthService, useValue: { logout: () => undefined, isAdmin: isAdmin.asReadonly() } }],
    }).compileComponents();
    fixture = TestBed.createComponent(Arc2StudioComponent);
    http = TestBed.inject(HttpTestingController);
    el = fixture.nativeElement;
  });

  /** Open the page; the server says whether the Studio is on before anything else is asked. */
  function open(enabled = true): void {
    fixture.detectChanges();
    http.expectOne(`${API}/status`).flush(
      enabled ? { enabled: true, reason: null } : { enabled: false, reason: 'ARC² Course Studio is not enabled on this server.' });
    fixture.detectChanges();
  }

  /** Stop polling (the page clears its timer on destroy) and drain what is left. */
  function finish(): void {
    fixture.destroy();
    tick(0);
    http.match(() => true).forEach(r => r.flush({}));
  }

  it('Send on a new project starts the course with the name and the description', fakeAsync(() => {
    open();
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
    open();
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

  it('marks a run whose gates a test host accepted by itself as test content, in the list and the run', fakeAsync(() => {
    const auto = detail({
      phase: 'preview', phase_text: 'Preview ready for your review', auto_accepted: true,
      gates: { outline: { state: 'accepted', accepted_by: 'auto (test host)' }, preview: { state: 'pending' } },
      messages: [
        { role: 'user', text: '60 min beginner course', ts: null },
        { role: 'pipeline', text: 'Outline accepted automatically by the runner (test host). Not reviewed by a person.', ts: null, auto: true },
      ],
    });
    open();
    http.expectOne(`${API}/runs`).flush({ runs: [auto], runner_seen: null });
    http.expectOne(`${API}/runs/arc2-wireshark`).flush(auto);
    fixture.detectChanges();
    tick();
    fixture.detectChanges();
    expect(el.querySelector('.proj .tag.test')!.textContent).toContain('TEST CONTENT');
    const badge = el.querySelector('.testbadge[role="note"]')!;
    expect(badge.textContent).toContain('TEST CONTENT — gates auto-accepted, not reviewed');
    const note = el.querySelector('.msg.pipe.auto')!;
    expect(note.textContent).toContain('AUTO-ACCEPTED (TEST HOST)');
    expect(note.textContent).toContain('Not reviewed by a person');
    finish();
  }));

  it('a run a person reviewed carries no test-content badge', fakeAsync(() => {
    open();
    http.expectOne(`${API}/runs`).flush({ runs: [detail()], runner_seen: null });
    http.expectOne(`${API}/runs/arc2-wireshark`).flush(detail({ auto_accepted: false }));
    fixture.detectChanges();
    tick();
    fixture.detectChanges();
    expect(el.querySelector('.testbadge')).toBeNull();
    expect(el.querySelector('.proj .tag.test')).toBeNull();
    expect(el.textContent).not.toContain('TEST CONTENT');
    finish();
  }));

  it('shows the engine’s refusal instead of failing silently', fakeAsync(() => {
    open();
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

  it('when the server has it off: says so, offers no New Project and calls nothing else', fakeAsync(() => {
    open(false);
    expect(el.textContent).toContain('ARC² Course Studio isn’t enabled on this server');
    expect(el.textContent).toContain('ARC² Course Studio is not enabled on this server.');
    expect(el.querySelector('.btn-new')).toBeNull();
    expect(el.querySelector('.send')).toBeNull();
    expect(el.textContent).not.toContain('tn_arc2_enabled');
    http.expectNone(`${API}/runs`);
    http.verify();
    fixture.destroy();
  }));

  it('tells an administrator how to turn it on', fakeAsync(() => {
    isAdmin.set(true);
    open(false);
    expect(el.querySelector('.admin-hint')!.textContent).toContain('tn_arc2_enabled');
    expect(el.textContent).toContain('docs/arc2-course-studio.md');
    http.verify();
    fixture.destroy();
  }));

  it('when on, shows the projects and New Project as before', fakeAsync(() => {
    open();
    http.expectOne(`${API}/runs`).flush({ runs: [], runner_seen: null });
    fixture.detectChanges();
    expect(el.querySelector('.btn-new')).not.toBeNull();
    expect(el.textContent).not.toContain('isn’t enabled');
    finish();
  }));

  it('a failed project list shows the server’s message', fakeAsync(() => {
    open();
    http.expectOne(`${API}/runs`).flush({ detail: 'Not Found' }, { status: 404, statusText: 'Not Found' });
    fixture.detectChanges();
    expect(el.querySelector('[role="alert"]')!.textContent).toContain('Couldn’t load projects: Not Found');
    finish();
  }));
});
