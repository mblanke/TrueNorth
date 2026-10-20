import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse } from '@angular/common/http';
import { MatDialog } from '@angular/material/dialog';
import { of, throwError } from 'rxjs';
import { ApiService, ExerciseClass } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { fakeAuth } from '@core/auth/fake-auth';
import { LiveClassComponent } from './live-class.component';
import { ago, lanesOf, sortStudents, statusesOf } from './live-class';

const STARTED = '2026-10-20T09:00:00Z';
const NOW = '2026-10-20T10:12:00Z';

function student(over: Record<string, unknown>) {
  return {
    user_id: 'u-x', display_name: 'X', team: null, labs: [], attempts_total: 0, attempts_credited: 0,
    credited_refs: [], score: 0, current_objective: 'detect_c2', last_detection_at: null, last_activity_at: null,
    objectives: [{ ref_id: 'detect_c2', state: 'not_attempted', misses: 0 }], attempts: [], attention: [],
    ...over,
  };
}

const CLASS = {
  exercise_id: 'ex-1', exercise_name: 'VIGILANT MAPLE', state: 'running', started_at: STARTED, completed_at: null,
  range: { id: 'r-1', name: 'r', state: 'ready', error: null }, max_score: 50,
  objectives: [{ ref_id: 'detect_c2', description: '', objective_type: 'detection', validator: 'opensearch_query', points: 40 }],
  learning_signal_misses: 3, not_seen_minutes: 15, generated_at: NOW,
  students: [
    student({
      user_id: 'u-okafor', display_name: 'Okafor', team: { id: 't1', name: 'Blue Cell 1', color_hex: '#1E5AA8' },
      attempts_total: 3, last_activity_at: '2026-10-20T10:05:00Z',
      objectives: [{ ref_id: 'detect_c2', state: 'missed', misses: 3 }],
      attempts: [1, 2, 3].map(n => ({ id: `a${n}`, objective_ref: 'detect_c2', verdict: 'missed', submitted_at: `2026-10-20T09:4${n}:00Z`, events_matched: 4, query: `process.name:x${n}`, precision: 0.25 })),
      attention: [{ kind: 'learning', detail: '3 judged misses on detect_c2 and no credit on it yet', objective_ref: 'detect_c2', since: '2026-10-20T09:41:00Z' }],
    }),
    student({
      user_id: 'u-lee', display_name: 'Lee', attempts_total: 1, attempts_credited: 1, credited_refs: ['detect_c2'], score: 40,
      current_objective: null, last_activity_at: '2026-10-20T10:10:00Z',
      labs: [{ session_id: 'lab-lee', activity_id: 'mod_003', state: 'active', error: '', attempt: 1, last_seen_at: '2026-10-20T10:10:00Z' }],
      objectives: [{ ref_id: 'detect_c2', state: 'credited', misses: 0, credited_at: '2026-10-20T09:50:00Z' }],
    }),
    student({
      user_id: 'u-ng', display_name: 'Ng',
      labs: [{ session_id: 'lab-ng', activity_id: 'mod_003', state: 'failed', error: 'clone timed out', attempt: 1 }],
      attention: [{ kind: 'lab_fault', detail: 'Lab failed: clone timed out', since: '2026-10-20T09:30:00Z' }],
    }),
    student({
      user_id: 'u-ito', display_name: 'Ito',
      attention: [{ kind: 'not_seen', detail: 'No activity seen since the start (72 min)', since: STARTED }],
    }),
  ],
} as unknown as ExerciseClass;

describe('live class helpers', () => {
  const students = CLASS.students!;

  it('puts each attention item in its own lane and leaves help requests empty (F2)', () => {
    const lanes = lanesOf(students);
    expect(lanes.help).toEqual([]);
    expect(lanes.lab.map(i => i.student.display_name)).toEqual(['Ng']);
    expect(lanes.learning.map(i => i.student.display_name)).toEqual(['Okafor']);
    expect(lanes.quiet.map(i => i.student.display_name)).toEqual(['Ito']);
  });

  it('ranks a lab fault above a learning signal above not seen, and "ok" when nothing is flagged', () => {
    expect(statusesOf(students[2])).toEqual(['lab_fault']);
    expect(statusesOf(students[1])).toEqual(['ok']);
    const sorted = sortStudents(students, 'status', 'asc').map(s => s.display_name);
    expect(sorted).toEqual(['Ng', 'Okafor', 'Ito', 'Lee']);
  });

  it('sorts by detections credited and by least recent activity', () => {
    expect(sortStudents(students, 'detections', 'desc')[0].display_name).toBe('Lee');
    expect(sortStudents(students, 'activity', 'asc').map(s => s.display_name)).toEqual(['Ito', 'Ng', 'Okafor', 'Lee']);
  });

  it('says how long ago without inventing a time', () => {
    const now = new Date(NOW);
    expect(ago(null, now)).toBe('never');
    expect(ago('2026-10-20T10:05:00Z', now)).toBe('7 min ago');
    expect(ago('2026-10-20T09:00:00Z', now)).toBe('1 h 12 min ago');
  });
});

describe('LiveClassComponent', () => {
  let fixture: ComponentFixture<LiveClassComponent>;
  let api: jasmine.SpyObj<ApiService>;
  let notify: jasmine.SpyObj<NotificationService>;
  let dialog: { open: jasmine.Spy };
  let auth: ReturnType<typeof fakeAuth>;
  const el = () => fixture.nativeElement as HTMLElement;

  async function make(role: Parameters<typeof fakeAuth>[0] = 'instructor') {
    auth = fakeAuth(role);
    api = jasmine.createSpyObj('ApiService', ['getExerciseClass', 'listInjects', 'resetLabSession']);
    api.getExerciseClass.and.returnValue(of(structuredClone(CLASS)));
    api.listInjects.and.returnValue(of([
      { id: 'i1', source: 'timeline', seq: 1, action: 'phish', status: 'fired', created_at: '2026-10-20T09:10:00Z' },
      { id: 'i2', source: 'timeline', seq: 2, action: 'smb copy', status: 'failed', created_at: '2026-10-20T09:55:00Z' },
    ] as any));
    notify = jasmine.createSpyObj('NotificationService', ['success', 'error']);
    dialog = { open: jasmine.createSpy('open').and.returnValue({ afterClosed: () => of(true) }) };
    await TestBed.configureTestingModule({
      imports: [LiveClassComponent, NoopAnimationsModule],
      providers: [
        { provide: ApiService, useValue: api },
        { provide: AuthService, useValue: auth },
        { provide: NotificationService, useValue: notify },
        { provide: MatDialog, useValue: dialog },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(LiveClassComponent);
    fixture.componentRef.setInput('exerciseId', 'ex-1');
    fixture.detectChanges();
  }

  afterEach(() => fixture?.destroy());

  it('renders the four lanes, the help lane as F2-future with no fake requests', async () => {
    await make();
    const help = el().querySelector('[data-testid="lane-help"]')!;
    expect(help.textContent).toContain('Help requests arrive with the intervention console (R1 F2)');
    expect(help.querySelectorAll('.qcard').length).toBe(0);
    const learning = el().querySelector('[data-testid="lane-learning"]')!.textContent!;
    expect(learning).toContain('Okafor');
    expect(learning).toContain('3 judged misses on Detect C2');  // the objective's name, not its ref
    expect(learning).not.toContain('detect_c2');
    expect(el().querySelector('[data-testid="lane-lab"]')!.textContent).toContain('clone timed out');
    expect(el().querySelector('[data-testid="lane-quiet"]')!.textContent).toContain('Ito');
    expect(el().textContent!.toLowerCase()).not.toContain('struggl');
  });

  it('lists every rostered Student with team, status, detections and last activity', async () => {
    await make();
    const rows = Array.from(el().querySelectorAll('table.dense tbody tr'));
    expect(rows.length).toBe(4);
    const okafor = rows.find(r => r.textContent!.includes('Okafor'))!;
    expect(okafor.textContent).toContain('Blue Cell 1');
    expect(okafor.textContent).toContain('Learning signal');
    expect(okafor.textContent).toContain('0 / 3');
    expect(okafor.textContent).toContain('7 min ago');
    expect(el().querySelector('[data-testid="class-meta"]')!.textContent).toContain('4 Students');
  });

  it('sorts by a column header and says so to assistive technology', async () => {
    await make();
    const nameHeader = Array.from(el().querySelectorAll('th')).find(th => th.textContent!.includes('Student'))!;
    nameHeader.querySelector('button')!.click();
    fixture.detectChanges();
    expect(nameHeader.getAttribute('aria-sort')).toBe('ascending');
    const first = el().querySelector('table.dense tbody tr')!;
    expect(first.textContent).toContain('Ito');
  });

  it('opens the Student drawer from the keyboard with attempts and objectives; F2 actions are not live', async () => {
    await make();
    const row = Array.from(el().querySelectorAll<HTMLTableRowElement>('table.dense tbody tr')).find(r => r.textContent!.includes('Okafor'))!;
    row.focus();
    row.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    fixture.detectChanges();
    await new Promise(r => setTimeout(r));
    expect(document.activeElement?.getAttribute('aria-label')).toBe('Close Okafor');
    const drawer = document.querySelector('[data-testid="student-drawer"]')!;
    expect(drawer.getAttribute('role')).toBe('dialog');
    expect(drawer.textContent).toContain('Detection attempts');
    expect(drawer.textContent).toContain('process.name:x3');
    expect(drawer.textContent).toContain('3 missed');
    const message = Array.from(drawer.querySelectorAll('button')).find(b => b.textContent!.includes('Message'))!;
    expect(message.getAttribute('aria-disabled')).toBe('true');
    expect(message.getAttribute('aria-label')).toContain('Coming in R1 F2');
    // Okafor has no lab of their own: no reset offered.
    expect(Array.from(drawer.querySelectorAll('button')).some(b => b.textContent!.includes('Reset lab'))).toBeFalse();
    drawer.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    fixture.detectChanges();
    await new Promise(r => setTimeout(r));
    expect(document.querySelector('[data-testid="student-drawer"]')).toBeNull();
    expect(document.activeElement).toBe(row);  // focus goes back where it came from
  });

  it('resets a ready lab only after confirmation, and shows the outcome the API reports', async () => {
    await make();
    api.resetLabSession.and.returnValue(of({ state: 'resetting' } as any));
    const lee = fixture.componentInstance.students().find(s => s.display_name === 'Lee')!;
    fixture.componentInstance.open(lee);
    fixture.detectChanges();
    const reset = Array.from(document.querySelectorAll('[data-testid="student-drawer"] button')).find(b => b.textContent!.includes('Reset lab'))!;
    (reset as HTMLButtonElement).click();
    expect(dialog.open).toHaveBeenCalled();
    expect(api.resetLabSession).toHaveBeenCalledWith('lab-lee');
    expect(notify.success).toHaveBeenCalledWith("Reset requested; Lee's lab for mod_003 is resetting.");
    expect(api.getExerciseClass).toHaveBeenCalledTimes(2);  // re-read, not assumed
  });

  it('does not reset when the confirmation is cancelled, and reports a refusal as a failure', async () => {
    await make();
    const lee = fixture.componentInstance.students().find(s => s.display_name === 'Lee')!;
    dialog.open.and.returnValue({ afterClosed: () => of(false) });
    fixture.componentInstance.confirmReset(lee, lee.labs![0]);
    expect(api.resetLabSession).not.toHaveBeenCalled();
    dialog.open.and.returnValue({ afterClosed: () => of(true) });
    api.resetLabSession.and.returnValue(throwError(() => new HttpErrorResponse({ status: 409, error: { detail: 'the lab is failed' } })));
    fixture.componentInstance.confirmReset(lee, lee.labs![0]);
    expect(notify.error).toHaveBeenCalledWith('The lab was not reset: the lab is failed');
    expect(notify.success).not.toHaveBeenCalled();
  });

  it('lists every lab of a Student with several, each with its own confirmed Reset (never a guessed one)', async () => {
    await make();
    const two = structuredClone(CLASS);
    const lee = two.students!.find(s => s.display_name === 'Lee')!;
    lee.labs = [
      { session_id: 'lab-b', activity_id: 'mod_004', state: 'failed', error: 'no capacity', attempt: 1 },
      { session_id: 'lab-a', activity_id: 'mod_003', state: 'active', error: '', attempt: 2 },
    ] as any;
    api.getExerciseClass.and.returnValue(of(two));
    api.resetLabSession.and.returnValue(of({ state: 'resetting' } as any));
    fixture.componentInstance.refresh();
    fixture.detectChanges();
    fixture.componentInstance.open(fixture.componentInstance.students().find(s => s.display_name === 'Lee')!);
    fixture.detectChanges();
    const drawer = document.querySelector('[data-testid="student-drawer"]')!;
    expect(drawer.querySelector('#dr-lab')!.textContent).toContain('2 labs');
    const labs = Array.from(drawer.querySelectorAll('[data-testid="lab"]'));
    expect(labs.length).toBe(2);
    expect(labs[0].textContent).toContain('lab for mod_004');
    expect(labs[0].textContent).toContain('no capacity');
    expect(labs[0].querySelector('button')!.getAttribute('aria-disabled')).toBe('true');  // the API refuses a failed lab
    (labs[1].querySelector('button') as HTMLButtonElement).click();
    expect(dialog.open.calls.mostRecent().args[1].data.title).toBe("Reset Lee's lab for mod_003 (attempt 2)?");
    expect(api.resetLabSession).toHaveBeenCalledOnceWith('lab-a');
  });

  it('offers no reset of a failed lab (the API refuses it) and none to an observer', async () => {
    await make();
    const ng = fixture.componentInstance.students().find(s => s.display_name === 'Ng')!;
    fixture.componentInstance.open(ng);
    fixture.detectChanges();
    const reset = Array.from(document.querySelectorAll('[data-testid="student-drawer"] button')).find(b => b.textContent!.includes('Reset lab'))!;
    expect(reset.getAttribute('aria-disabled')).toBe('true');
    fixture.destroy();
    TestBed.resetTestingModule();
    await make('observer');
    const lee = fixture.componentInstance.students().find(s => s.display_name === 'Lee')!;
    fixture.componentInstance.open(lee);
    fixture.detectChanges();
    expect(Array.from(document.querySelectorAll('[data-testid="student-drawer"] button')).some(b => b.textContent!.includes('Reset lab'))).toBeFalse();
  });

  it('shows a refusal as an error, never as an empty class', async () => {
    auth = fakeAuth('instructor');
    await make();
    fixture.destroy();
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [LiveClassComponent, NoopAnimationsModule],
      providers: [
        { provide: ApiService, useValue: { getExerciseClass: () => throwError(() => new HttpErrorResponse({ status: 403 })), listInjects: () => of([]) } },
        { provide: AuthService, useValue: fakeAuth('instructor') },
        { provide: NotificationService, useValue: notify },
        { provide: MatDialog, useValue: dialog },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(LiveClassComponent);
    fixture.componentRef.setInput('exerciseId', 'ex-1');
    fixture.detectChanges();
    expect(el().querySelector('[role="alert"]')!.textContent).toContain('You do not have access to this class roster.');
    expect(el().querySelector('table.dense')).toBeNull();
    expect(el().textContent).not.toContain('0 Students');
  });

  it('keeps the last good class when a refresh fails, and says it is stale', async () => {
    await make();
    api.getExerciseClass.and.returnValue(throwError(() => new HttpErrorResponse({ status: 0 })));
    fixture.componentInstance.refresh();
    fixture.detectChanges();
    expect(el().textContent).toContain('Could not refresh (The server could not be reached.)');
    expect(el().querySelectorAll('table.dense tbody tr').length).toBe(4);
  });

  it('draws the run\'s injects on the timeline, a failed one marked as the exercise\'s', async () => {
    await make();
    const marks = el().querySelectorAll('tn-inject-timeline button');
    expect(marks.length).toBe(2);
    expect(marks[1].getAttribute('aria-label')).toContain('T+0:55 smb copy, failed');
    (marks[1] as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(el().querySelector('tn-inject-timeline .detail')!.textContent).toContain('not counted against any Student');
  });
});
