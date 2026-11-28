// Author: Marc-Andre Blanke
// Created: 2026-11-27T00:00:00Z
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { Subject, of, throwError } from 'rxjs';
import {
  StudentList, StudentProgressApiService, StudentRecord,
} from '@core/services/student-progress-api.service';
import { NotificationService } from '@core/services/notification.service';
import {
  StudentProgressComponent, attendanceShort, levelText, resultsText,
} from './student-progress.component';

const ANN = {
  user_id: 'u1', display_name: 'Cpl Ann Ibrahim', email: 'ann@corp.test', rank: 'Cpl', unit: 'Cyber Command',
  callsign: 'Raven-12', proficiency: 'advanced', classes: 2, present: 3, excused: 0, absent: 1,
  passed: 1, failed: 0, incomplete: 0, awaiting_result: 1, qualifications: ['ALJQ'],
};
const BOB = {
  ...ANN, user_id: 'u2', display_name: 'S1 Bob Chen', email: 'bob@corp.test', rank: 'S1', unit: 'Fleet Cyber East',
  proficiency: null, classes: 0, present: 0, absent: 0, passed: 0, awaiting_result: 0, qualifications: [],
};
const LIST = { units: ['Cyber Command', 'Fleet Cyber East'], students: [ANN, BOB] } as unknown as StudentList;
const RECORD = {
  ...ANN,
  competencies: [{ category: 'Analyze', proficiency: 'expert', assertions: 2 }],
  class_history: [{
    class_id: 'c1', name: 'C201-26-03', course_name: 'SOC Analyst', qualification: 'ALJQ',
    start_date: '2026-10-05', end_date: '2026-10-09', status: 'closed', sessions_held: 4,
    present: 3, excused: 0, absent: 1, outcome: 'pass', comment: 'Solid triage', decided_at: null,
    qualification_earned: 'ALJQ',
  }],
} as unknown as StudentRecord;

describe('Students page helpers', () => {
  it('names a level, never a score', () => {
    expect(levelText('intermediate')).toBe('Intermediate');
    expect(levelText(null)).toBe('-');
  });

  it('counts attendance without a percentage', () => {
    expect(attendanceShort({ present: 3, excused: 0, absent: 1 })).toBe('3 of 4 present');
    expect(attendanceShort({ present: 2, excused: 1, absent: 1 })).toBe('2 of 4 present, 1 excused');
    expect(attendanceShort({ present: 0, excused: 0, absent: 0 })).toBe('None recorded');
  });

  it('sums results in words', () => {
    expect(resultsText({ passed: 1, failed: 0, incomplete: 0, awaiting_result: 1 })).toBe('1 pass, 1 awaiting');
    expect(resultsText({ passed: 0, failed: 0, incomplete: 0, awaiting_result: 0 })).toBe('No classes');
  });
});

describe('StudentProgressComponent', () => {
  let fixture: ComponentFixture<StudentProgressComponent>;
  let api: jasmine.SpyObj<StudentProgressApiService>;
  let notify: jasmine.SpyObj<NotificationService>;

  beforeEach(async () => {
    api = jasmine.createSpyObj('StudentProgressApiService', ['list', 'record']);
    notify = jasmine.createSpyObj('NotificationService', ['error']);
    api.list.and.returnValue(of(LIST));
    api.record.and.returnValue(of(RECORD));
    await TestBed.configureTestingModule({
      imports: [StudentProgressComponent, NoopAnimationsModule],
      providers: [
        { provide: StudentProgressApiService, useValue: api },
        { provide: NotificationService, useValue: notify },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(StudentProgressComponent);
    fixture.detectChanges();
  });

  function text(): string {
    return (fixture.nativeElement as HTMLElement).textContent ?? '';
  }

  it('lists every Student with formation, level, attendance, results and qualifications', () => {
    expect(api.list).toHaveBeenCalledWith({ unit: '', q: '' });
    const rows = fixture.nativeElement.querySelectorAll('tr.student-row');
    expect(rows.length).toBe(2);
    const first = rows[0].textContent;
    for (const bit of ['Cpl Ann Ibrahim', 'Cyber Command', 'Advanced', '3 of 4 present', '1 pass, 1 awaiting', 'ALJQ']) {
      expect(first).toContain(bit);
    }
    expect(rows[1].textContent).toContain('None recorded');
    expect(text()).toContain('2 Students');
    expect(text()).not.toContain('%');
  });

  it('filters by formation and search', () => {
    const c = fixture.componentInstance;
    c.filter = { unit: 'Fleet Cyber East', q: 'bob' };
    c.load();
    expect(api.list).toHaveBeenCalledWith({ unit: 'Fleet Cyber East', q: 'bob' });
  });

  it('opens a Student record with competency by category and each class', () => {
    (fixture.nativeElement.querySelector('tr.student-row') as HTMLElement).click();
    fixture.detectChanges();
    expect(api.record).toHaveBeenCalledWith('u1');
    const record = fixture.nativeElement.querySelector('.record') as HTMLElement;
    for (const bit of ['Proficiency: Advanced', 'Analyze', 'Expert', 'C201-26-03', 'Pass', 'Solid triage', 'earned ALJQ']) {
      expect(record.textContent).toContain(bit);
    }
    (record.querySelector('button.close') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.record')).toBeNull();
  });

  it('shows the last Student clicked, dropping an earlier record still loading', () => {
    const slow = new Subject<StudentRecord>();
    api.record.and.returnValues(slow, of({ ...RECORD, display_name: 'S1 Bob Chen' } as unknown as StudentRecord));
    const rows = fixture.nativeElement.querySelectorAll('tr.student-row');
    (rows[0] as HTMLElement).click();
    (rows[1] as HTMLElement).click();
    slow.next(RECORD); // Ann's answer arrives late and is ignored
    fixture.detectChanges();
    expect(fixture.componentInstance.selected()?.display_name).toBe('S1 Bob Chen');
  });

  it('shows an empty state when nothing matches', () => {
    api.list.and.returnValue(of({ units: [], students: [] } as unknown as StudentList));
    fixture.componentInstance.load();
    fixture.detectChanges();
    expect(text()).toContain('No Student matches these filters.');
  });

  it('reports an API error instead of showing stale numbers', () => {
    api.list.and.returnValue(throwError(() => ({ status: 403, error: { detail: 'Forbidden' } })));
    fixture.componentInstance.load();
    expect(notify.error).toHaveBeenCalled();
    expect(fixture.componentInstance.loading()).toBeFalse();
  });
});
