import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { MatSnackBar } from '@angular/material/snack-bar';
import { of, throwError } from 'rxjs';
import { CurriculumForgeComponent } from './curriculum-forge.component';
import { ApiService } from '@core/services/api.service';

describe('CurriculumForgeComponent', () => {
  let component: CurriculumForgeComponent;
  let fixture: ComponentFixture<CurriculumForgeComponent>;
  let api: jasmine.SpyObj<ApiService>;
  let snack: jasmine.SpyObj<MatSnackBar>;

  const ready = { id: 'c-1', name: 'SOC Analyst L1', status: 'ready', documents: [] };

  beforeEach(async () => {
    api = jasmine.createSpyObj('ApiService', [
      'listCurricula', 'getCurriculum', 'listQuizzes', 'generateCourseFromCurriculum', 'quizExportUrl',
    ]);
    snack = jasmine.createSpyObj('MatSnackBar', ['open']);
    api.listCurricula.and.returnValue(of([ready]));
    api.listQuizzes.and.returnValue(of([]));

    await TestBed.configureTestingModule({
      imports: [CurriculumForgeComponent, NoopAnimationsModule],
      providers: [provideRouter([]), { provide: ApiService, useValue: api }],
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
});
