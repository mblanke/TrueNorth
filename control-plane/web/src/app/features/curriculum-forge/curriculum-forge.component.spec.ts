import { WritableSignal, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { MatSnackBar } from '@angular/material/snack-bar';
import { of, throwError } from 'rxjs';
import { CurriculumForgeComponent } from './curriculum-forge.component';
import { ApiService } from '@core/services/api.service';
import { AuthService } from '@core/services/auth.service';

describe('CurriculumForgeComponent', () => {
  let component: CurriculumForgeComponent;
  let fixture: ComponentFixture<CurriculumForgeComponent>;
  let api: jasmine.SpyObj<ApiService>;
  let snack: jasmine.SpyObj<MatSnackBar>;
  let canAuthor: WritableSignal<boolean>;

  const ready = { id: 'c-1', name: 'SOC Analyst L1', status: 'ready', documents: [] };

  beforeEach(async () => {
    api = jasmine.createSpyObj('ApiService', [
      'listCurricula', 'getCurriculum', 'listQuizzes', 'generateCourseFromCurriculum', 'quizExportUrl',
    ]);
    snack = jasmine.createSpyObj('MatSnackBar', ['open']);
    api.listCurricula.and.returnValue(of([ready]));
    api.listQuizzes.and.returnValue(of([]));
    api.quizExportUrl.and.callFake((id: string, f: string) => `/api/v1/quizzes/${id}/export?format=${f}`);
    canAuthor = signal(true);

    await TestBed.configureTestingModule({
      imports: [CurriculumForgeComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ApiService, useValue: api },
        { provide: AuthService, useValue: { canAuthorCourses: canAuthor } },
      ],
    })
      // The component imports MatSnackBarModule, whose provider would shadow a plain one.
      .overrideProvider(MatSnackBar, { useValue: snack })
      .compileComponents();

    fixture = TestBed.createComponent(CurriculumForgeComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('lists curricula on init', () => {
    fixture.detectChanges();
    expect(api.listCurricula).toHaveBeenCalled();
    expect(component.curricula().map(c => c.id)).toEqual(['c-1']);
  });

  it('generateCourse posts the Course Studio options and keeps the draft', () => {
    const draft = { course_id: 'course-9', name: 'SOC basics', module_count: 6, quiz_module_count: 1, model_used: 'mock' };
    api.generateCourseFromCurriculum.and.returnValue(of(draft));
    component.select(ready);
    component.courseDifficulty = 'advanced';
    component.courseModules = 4;
    component.courseFocus = 'triage';

    component.generateCourse();

    expect(api.generateCourseFromCurriculum).toHaveBeenCalledWith('c-1', {
      difficulty: 'advanced', module_count: 4, focus: 'triage',
    });
    expect(component.lastCourse()).toEqual(draft);
    expect(component.generatingCourse()).toBeFalse();
  });

  it('generateCourse reports an orchestrator outage and clears the spinner', () => {
    api.generateCourseFromCurriculum.and.returnValue(
      throwError(() => ({ error: { detail: 'AI orchestrator is unavailable.' } })));
    component.select(ready);

    component.generateCourse();

    expect(component.generatingCourse()).toBeFalse();
    expect(component.lastCourse()).toBeNull();
    expect(snack.open.calls.mostRecent().args[0]).toBe('AI orchestrator is unavailable.');
  });

  it('generateCourse does nothing without a selected curriculum', () => {
    component.generateCourse();
    expect(api.generateCourseFromCurriculum).not.toHaveBeenCalled();
  });

  describe('quiz authoring controls (course:author)', () => {
    const quizzes = [
      { id: 'q-draft', title: 'Draft quiz', is_published: false, question_count: 5, pass_pct: 70 },
      { id: 'q-live', title: 'Live quiz', is_published: true, question_count: 8, pass_pct: 70 },
    ];

    function renderSelected(): HTMLElement {
      api.listQuizzes.and.returnValue(of(quizzes));
      fixture.detectChanges();
      component.select(ready);
      fixture.detectChanges();
      return fixture.nativeElement as HTMLElement;
    }
    const buttonTexts = (el: HTMLElement) =>
      Array.from(el.querySelectorAll('button, a')).map(b => (b.textContent ?? '').trim());

    it('shows Generate quiz, Publish and exports to an author', () => {
      const el = renderSelected();
      const texts = buttonTexts(el);
      expect(el.textContent).toContain('Generate quiz');
      expect(texts.some(t => t.includes('Generate quiz with AI'))).toBeTrue();
      expect(texts.some(t => t.includes('Publish'))).toBeTrue();
      expect(texts.some(t => t.includes('Moodle XML'))).toBeTrue();
    });

    it('hides Generate quiz, Publish and exports from a Student, keeping Take', () => {
      canAuthor.set(false);
      const el = renderSelected();
      const texts = buttonTexts(el);
      expect(texts.some(t => t.includes('Generate quiz with AI'))).toBeFalse();
      expect(texts.some(t => t.includes('Publish'))).toBeFalse();
      expect(texts.some(t => t.includes('GIFT') || t.includes('Moodle XML'))).toBeFalse();
      expect(texts.some(t => t.includes('Take'))).toBeTrue();
    });
  });
});
