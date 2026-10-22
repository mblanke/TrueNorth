import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse } from '@angular/common/http';
import { signal } from '@angular/core';
import { of, throwError } from 'rxjs';
import { AuthService, UserRole } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import {
  CertificationEntry, CertificationSection, RangeCertification, RangeCertificationApiService,
} from '@core/services/range-certification-api.service';
import { RangeCertificationComponent } from './range-certification.component';

function section(name: string, title: string, status: CertificationSection['status'], summary = '',
                 historyCount = status === 'missing' ? 0 : 1): CertificationSection {
  return { name, title, status, summary, required: name !== 'overrides', provided_by: 'S4 (#242)',
           updated_at: status === 'missing' ? null : '2026-10-21T12:00:00Z', history_count: historyCount, entries: [] };
}

function entry(seq: number, status: CertificationEntry['status'], summary: string): CertificationEntry {
  return { seq, section: 'teardown', status, summary, detail: {}, source: 'api.capture', actor: 'system',
           recorded_at: '2026-10-21T12:00:00Z' };
}

const READY: RangeCertification = {
  range_id: 'r1',
  retention_months: 12,
  archived: [{ deployment_id: 'd0', generation: 1, opened_at: '2026-09-01T11:00:00Z', verdict: 'not_safe',
               verdict_label: 'Not safe', purge_after: '2027-09-01T11:00:00Z' }],
  record: {
    id: 'rec1', deployment_id: 'd1', generation: 2, range_id: 'r1', range_name: 'Cert Range', template_id: null,
    template_name: 'cert-lab', template_version: '3.1', plan_hash: 'a'.repeat(64),
    plan_hash_basis: 'template_yaml_sha256', opened_by: 'Test instructor', opened_by_id: null,
    opened_at: '2026-10-21T11:00:00Z', tenant_id: 't1', deployment_status: 'failed', verdict: 'ready_not_certified', verdict_label: 'Ready, not certified',
    reasons: ['Isolation evidence missing (provided by S4 (#242))'],
    history: false,
    sections: [
      section('guest_readiness', 'Guest readiness', 'pass', '2 of 2 VM(s) verified by VMware Tools'),
      section('network_allocation', 'Network allocation', 'incomplete', 'external-collision check not recorded (S1)'),
      section('isolation', 'Isolation', 'missing'),
      section('teardown', 'Teardown', 'fail', 'destroy failed', 2),
      section('overrides', 'Approvals, overrides and forces', 'info'),
    ],
  },
};

describe('RangeCertificationComponent', () => {
  let fixture: ComponentFixture<RangeCertificationComponent>;
  let el: HTMLElement;
  let api: jasmine.SpyObj<RangeCertificationApiService>;
  let notify: jasmine.SpyObj<NotificationService>;

  async function render(role: UserRole, response: RangeCertification | HttpErrorResponse = READY) {
    api = jasmine.createSpyObj('RangeCertificationApiService', ['get', 'capture', 'export']);
    api.get.and.returnValue(response instanceof HttpErrorResponse ? throwError(() => response) : of(response));
    api.capture.and.returnValue(of({ deployment_id: 'd1', written: 3 }));
    api.export.and.returnValue(of(new Blob(['{}'], { type: 'application/json' })));
    notify = jasmine.createSpyObj('NotificationService', ['success', 'error', 'info']);
    await TestBed.configureTestingModule({
      imports: [RangeCertificationComponent, NoopAnimationsModule],
      providers: [
        { provide: RangeCertificationApiService, useValue: api },
        { provide: NotificationService, useValue: notify },
        { provide: AuthService, useValue: { user: signal({ id: 'u1', role }) } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(RangeCertificationComponent);
    fixture.componentRef.setInput('rangeId', 'r1');
    fixture.detectChanges();
    el = fixture.nativeElement;
  }

  const button = (label: string) =>
    Array.from(el.querySelectorAll('button')).find(b => (b.textContent ?? '').includes(label)) as HTMLButtonElement;
  const state = (name: string) =>
    el.querySelector(`tr[data-section="${name}"] .state`)?.getAttribute('data-status');

  it('shows staff the verdict, its reasons and every section state', async () => {
    await render('instructor');
    expect(api.get).toHaveBeenCalledOnceWith('r1');  // the summary: latest entry per section
    expect(el.querySelector('.badge')?.textContent).toContain('Ready, not certified');
    expect(el.querySelector('.badge')?.getAttribute('data-verdict')).toBe('ready_not_certified');
    expect(el.textContent).toContain('Isolation evidence missing');
    expect(state('guest_readiness')).toBe('pass');
    expect(state('network_allocation')).toBe('incomplete');
    expect(state('isolation')).toBe('missing');
    expect(state('teardown')).toBe('fail');
    expect(el.textContent).toContain('Not recorded yet (S4 (#242))');
    expect(el.textContent).toContain('opened');
    expect(el.querySelector('.build-note')?.textContent).toContain('Build failed');
  });

  for (const role of ['student', 'observer'] as UserRole[]) {
    it(`is not shown to a ${role}, and asks for nothing`, async () => {
      await render(role);
      expect(el.querySelector('section')).toBeNull();
      expect(api.get).not.toHaveBeenCalled();
    });
  }

  it('exports the newest deployment as signed JSON and as PDF', async () => {
    await render('range_ops');
    spyOn(URL, 'createObjectURL').and.returnValue('blob:x');
    spyOn(URL, 'revokeObjectURL');
    spyOn(HTMLAnchorElement.prototype, 'click');
    button('Export JSON').click();
    expect(api.export).toHaveBeenCalledWith('r1', 'json', 'd1');
    button('Export PDF').click();
    expect(api.export).toHaveBeenCalledWith('r1', 'pdf', 'd1');
  });

  it('records evidence on request and reloads', async () => {
    await render('admin');
    button('Record evidence').click();
    expect(api.capture).toHaveBeenCalledOnceWith('r1');
    expect(notify.success).toHaveBeenCalledWith('3 evidence entries recorded');
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it('keeps superseded entries behind a collapsed History link', async () => {
    await render('instructor');
    const full: RangeCertification = { ...READY, record: { ...READY.record!, history: true, sections: [
      { ...READY.record!.sections[3], entries: [entry(4, 'fail', 'destroy failed'), entry(5, 'incomplete', 'destroyed')] },
    ] } };
    api.get.and.returnValue(of(full));
    expect(el.querySelector('.history')).toBeNull();
    const link = button('History (1 earlier entry)');
    expect(link.getAttribute('aria-expanded')).toBe('false');
    link.click();
    fixture.detectChanges();
    expect(api.get).toHaveBeenCalledWith('r1', { deploymentId: 'd1', history: true });
    expect(el.querySelector('.history')?.textContent).toContain('#4');
    expect(el.querySelector('.history')?.textContent).toContain('destroy failed');
  });

  it('lists earlier builds under Archived certifications, each exportable before it is deleted', async () => {
    await render('instructor');
    spyOn(URL, 'createObjectURL').and.returnValue('blob:x');
    spyOn(URL, 'revokeObjectURL');
    spyOn(HTMLAnchorElement.prototype, 'click');
    const archived = el.querySelector('details.archived') as HTMLDetailsElement;
    expect(archived.open).toBeFalse();
    expect(archived.querySelector('summary')?.textContent).toContain('Archived certifications (1)');
    expect(archived.textContent).toContain('deleted 12 months after');
    expect(archived.querySelector('tr[data-deployment="d0"]')?.textContent).toContain('Not safe');
    (archived.querySelector('tr[data-deployment="d0"] button') as HTMLButtonElement).click();
    expect(api.export).toHaveBeenCalledWith('r1', 'json', 'd0');
  });

  it('says when no build has a record yet, with no export', async () => {
    await render('instructor', { range_id: 'r1', record: null, archived: [], retention_months: 12 });
    expect(el.textContent).toContain('No build of this range has a certification record yet');
    expect(button('Export JSON')).toBeUndefined();
  });

  it('explains a refusal', async () => {
    await render('instructor', new HttpErrorResponse({ status: 404 }));
    expect(el.textContent).toContain('Range not found');
  });
});
