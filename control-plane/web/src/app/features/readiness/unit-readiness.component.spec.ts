import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { ReadinessApiService, UnitReadiness } from '@core/services/readiness-api.service';
import { NotificationService } from '@core/services/notification.service';
import { UnitReadinessComponent, attendanceText, resultText } from './unit-readiness.component';

const REPORT = {
  date_from: '2025-10-27', date_to: '2027-01-25', unit: null, units: ['2 Sig Regt', 'Kingston Det'],
  summary: {
    classes: 1, enrolments: 2, students: 2, sessions_held: 2, present: 3, excused: 1, absent: 0,
    passed: 1, failed: 1, incomplete: 0, awaiting_result: 0, qualifications_earned: 1,
  },
  qualifications: [{ qualification_id: 'q1', code: 'ALJQ', title: 'Cyber operator, junior', earned: 1 }],
  classes: [{
    class_id: 'c1', name: 'C201-26-03', course_id: 'k1', course_name: 'SOC Analyst', qualification: 'ALJQ',
    start_date: '2026-10-05', end_date: '2026-10-09', status: 'closed', closed_at: '2026-10-10T12:00:00Z',
    sessions_held: 2, enrolled: 2, passed: 1, failed: 1, incomplete: 0, awaiting_result: 0,
    students: [
      { entry_id: 'e1', email: 'ann@corp.test', display_name: 'Ann', unit: '2 Sig Regt', roll_call_status: 'signed_in',
        enrolment_status: 'completed', present: 2, excused: 0, absent: 0, outcome: 'pass', comment: 'Solid triage',
        qualification_earned: 'ALJQ' },
      { entry_id: 'e2', email: 'bob@corp.test', display_name: 'Bob', unit: '2 Sig Regt', roll_call_status: 'signed_in',
        enrolment_status: 'enrolled', present: 1, excused: 1, absent: 0, outcome: 'fail', comment: 'Missed the brief',
        qualification_earned: null },
    ],
  }],
} as unknown as UnitReadiness;

describe('readiness helpers', () => {
  it('says the result in words, or that it is not decided yet', () => {
    expect(resultText('pass')).toBe('Pass');
    expect(resultText('incomplete')).toBe('Incomplete');
    expect(resultText(null)).toBe('Awaiting result');
  });

  it('counts attendance without a percentage', () => {
    expect(attendanceText({ present: 1, excused: 1, absent: 2 })).toBe('1 present, 1 excused, 2 absent');
    expect(attendanceText({ present: 0, excused: 0, absent: 0 })).toBe('None recorded');
  });
});

describe('UnitReadinessComponent', () => {
  let fixture: ComponentFixture<UnitReadinessComponent>;
  let el: HTMLElement;
  let api: jasmine.SpyObj<ReadinessApiService>;

  beforeEach(() => {
    api = jasmine.createSpyObj('ReadinessApiService', ['get', 'exportCsv', 'exportPdf']);
    api.get.and.returnValue(of(REPORT));
    api.exportCsv.and.returnValue(of('class\n'));
    api.exportPdf.and.returnValue(of(new Blob(['%PDF'])));
    const notify = jasmine.createSpyObj('NotificationService', ['success', 'error', 'info']);
    TestBed.configureTestingModule({
      imports: [UnitReadinessComponent, NoopAnimationsModule],
      providers: [
        { provide: ReadinessApiService, useValue: api },
        { provide: NotificationService, useValue: notify },
      ],
    });
    fixture = TestBed.createComponent(UnitReadinessComponent);
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  it('loads the page for every unit by default', () => {
    expect(api.get).toHaveBeenCalledWith({ dateFrom: '', dateTo: '', unit: '' });
  });

  it('shows the summary, each class, its Students, results, comments and qualifications', () => {
    const text = el.textContent!;
    expect(text).toContain('C201-26-03');
    expect(text).toContain('SOC Analyst');
    expect(text).toContain('Ann');
    expect(text).toContain('Solid triage');
    expect(text).toContain('Missed the brief');
    expect(text).toContain('ALJQ');
    expect(text).toContain('Fail');
    expect(el.querySelectorAll('tr.student-row').length).toBe(2);
    expect(el.querySelector('.summary')!.textContent).toContain('1 earned');
  });

  it('is read-only: no inputs on the results, only filters and exports', () => {
    expect(el.querySelectorAll('.results input, .results textarea, .results mat-select').length).toBe(0);
  });

  it('filters by unit and date', () => {
    const c = fixture.componentInstance;
    c.filter = { dateFrom: '2026-01-01', dateTo: '2026-06-30', unit: '2 Sig Regt' };
    c.load();
    expect(api.get).toHaveBeenCalledWith({ dateFrom: '2026-01-01', dateTo: '2026-06-30', unit: '2 Sig Regt' });
  });

  it('exports CSV and PDF with the filters in force', () => {
    const c = fixture.componentInstance;
    spyOn(URL, 'createObjectURL').and.returnValue('blob:x');
    spyOn(URL, 'revokeObjectURL');
    spyOn(HTMLAnchorElement.prototype, 'click');
    c.filter = { dateFrom: '', dateTo: '', unit: 'Kingston Det' };
    c.exportCsv();
    expect(api.exportCsv).toHaveBeenCalledWith({ dateFrom: '', dateTo: '', unit: 'Kingston Det' });
    c.exportPdf();
    expect(api.exportPdf).toHaveBeenCalledWith({ dateFrom: '', dateTo: '', unit: 'Kingston Det' });
  });

  it('says so when no class matches', () => {
    api.get.and.returnValue(of({ ...REPORT, classes: [], qualifications: [] } as UnitReadiness));
    fixture.componentInstance.load();
    fixture.detectChanges();
    expect(el.textContent).toContain('No classes');
  });
});
