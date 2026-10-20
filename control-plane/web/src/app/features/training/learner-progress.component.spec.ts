import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { HttpErrorResponse } from '@angular/common/http';
import { signal } from '@angular/core';
import { MatDialog } from '@angular/material/dialog';
import { of, throwError } from 'rxjs';
import { AuthService, UserRole } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { OperatorRecordsApiService } from '@core/services/operator-records-api.service';
import { LearnerProgressComponent } from './learner-progress.component';

const COURSES = { items: [{ id: 'c1', name: 'SOC Analyst I', is_published: true }], total: 1, limit: 200, offset: 0 };
const COURSE = { id: 'c1', name: 'SOC Analyst I', modules: [{ id: 'm1', ordinal: 1, title: 'Logs' }] };
const ENROLLMENT = {
  id: 'e1', course_id: 'c1', user_id: 'u1', status: 'in_progress', enrolled_at: '2026-10-01T00:00:00Z',
  final_score: 0, max_score: 0, final_grade: null, created_at: '',
};
const PROGRESS = [{ id: 'p1', enrollment_id: 'e1', module_id: 'm1', status: 'completed', score: 90, max_score: 100,
  attempts: 1, completed_at: '2026-10-02T00:00:00Z' }];

describe('LearnerProgressComponent', () => {
  let fixture: ComponentFixture<LearnerProgressComponent>;
  let component: LearnerProgressComponent;
  let el: HTMLElement;
  let api: jasmine.SpyObj<OperatorRecordsApiService>;
  let dialogOpen: jasmine.Spy;
  let confirmed: boolean;

  async function render(role: UserRole, users: unknown = of([{ id: 'u1', display_name: 'Sam', email: 's@x.test' }])) {
    api = jasmine.createSpyObj('OperatorRecordsApiService', ['listCourses', 'getCourse', 'listEnrollments',
      'getProgress', 'markComplete', 'listUsers']);
    api.listCourses.and.returnValue(of(COURSES as any));
    api.getCourse.and.returnValue(of(COURSE as any));
    api.listEnrollments.and.returnValue(of([ENROLLMENT as any]));
    api.getProgress.and.returnValue(of(PROGRESS as any));
    api.listUsers.and.returnValue(users as any);
    confirmed = true;
    dialogOpen = jasmine.createSpy('open').and.callFake(() => ({ afterClosed: () => of(confirmed ? { reason: '' } : null) }));
    await TestBed.configureTestingModule({
      imports: [LearnerProgressComponent, NoopAnimationsModule],
      providers: [
        { provide: OperatorRecordsApiService, useValue: api },
        { provide: AuthService, useValue: { user: signal({ id: 'me', role }) } },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['success', 'error']) },
        { provide: MatDialog, useValue: { open: dialogOpen } },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(LearnerProgressComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    el = fixture.nativeElement;
    component.pick('c1');
    fixture.detectChanges();
  }

  const button = (text: string) =>
    Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes(text)) as HTMLButtonElement | undefined;

  it('lists a course\'s Students by name with their status', async () => {
    await render('instructor');
    expect(api.listEnrollments).toHaveBeenCalledWith('c1');
    expect(el.textContent).toContain('Sam (s@x.test)');
    expect(el.textContent).toContain('In progress');
    expect(el.textContent).not.toContain('in_progress');
  });

  // Issue #201: modules numbered from "0.", raw statuses, and Mark complete for a Student
  // who had done nothing.
  it('numbers modules from 1 and words their status', async () => {
    await render('instructor');
    component.course.set({ ...COURSE, modules: [
      { id: 'm1', ordinal: 0, title: 'Logs' }, { id: 'm2', ordinal: 1, title: 'Alerts' }] } as any);
    api.getProgress.and.returnValue(of([
      PROGRESS[0], { ...PROGRESS[0], id: 'p2', module_id: 'm2', status: 'in_progress', completed_at: null }] as any));
    button('Progress')!.click();
    fixture.detectChanges();
    const cells = Array.from(el.querySelectorAll('.inner tbody tr')).map(r => r.textContent ?? '');
    expect(cells[0]).toContain('1. Logs');
    expect(cells[0]).toContain('Completed');
    expect(cells[1]).toContain('2. Alerts');
    expect(cells[1]).toContain('In progress');
    expect(el.textContent).not.toContain('0. Logs');
    expect(el.textContent).not.toContain('in_progress');
  });

  it('offers Mark complete only for a Student with recorded progress', async () => {
    await render('instructor');
    api.listEnrollments.and.returnValue(of([{ ...ENROLLMENT, status: 'enrolled' }] as any));
    component.loadEnrollments();
    fixture.detectChanges();
    expect(button('Mark complete')).toBeUndefined();

    api.getProgress.and.returnValue(of([]));
    button('Progress')!.click();
    fixture.detectChanges();
    expect(el.textContent).toContain('No module started yet.');
    expect(button('Mark complete')).toBeUndefined();

    button('Hide')!.click();
    fixture.detectChanges();
    api.getProgress.and.returnValue(of(PROGRESS as any));
    button('Progress')!.click();
    fixture.detectChanges();
    expect(button('Mark complete')).toBeDefined();
  });

  it('opens one Student\'s module progress with module titles', async () => {
    await render('instructor');
    button('Progress')!.click();
    fixture.detectChanges();
    expect(api.getProgress).toHaveBeenCalledWith('c1', 'u1');
    expect(el.textContent).toContain('1. Logs');
    expect(el.textContent).toContain('90 / 100');
    expect(button('Hide')!.getAttribute('aria-expanded')).toBe('true');
  });

  it('marks complete only after the impact dialog, and shows the API-computed grade', async () => {
    await render('instructor');
    api.markComplete.and.returnValue(of({ ...ENROLLMENT, status: 'completed', final_score: 90, max_score: 100,
      final_grade: 'A' } as any));
    button('Mark complete')!.click();
    expect(dialogOpen.calls.mostRecent().args[1].data.impact.join(' ')).toContain('computed by the API');
    expect(api.markComplete).toHaveBeenCalledWith('c1', 'u1');
    fixture.detectChanges();
    expect(el.textContent).toContain('completed, 90 / 100, grade A');
  });

  it('a declined confirmation changes nothing', async () => {
    await render('admin');
    confirmed = false;
    button('Mark complete')!.click();
    expect(api.markComplete).not.toHaveBeenCalled();
  });

  it('roles without learning_record:write see no mark-complete', async () => {
    await render('observer');
    expect(button('Mark complete')).toBeUndefined();
  });

  it('falls back to ids, and says so, when names cannot be read', async () => {
    await render('instructor', throwError(() => new HttpErrorResponse({ status: 403 })));
    expect(el.textContent).toContain('Students are shown by id');
    expect(el.textContent).toContain('u1');
  });

  it('shows the API error for enrollments instead of an empty roster', async () => {
    await render('instructor');
    api.listEnrollments.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 403, error: { detail: 'Missing permission: learning_record:read' } })));
    component.loadEnrollments();
    fixture.detectChanges();
    expect(el.textContent).toContain('Missing permission: learning_record:read');
    expect(el.textContent).not.toContain('No Students');
  });
});
