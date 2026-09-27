import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';
import { ApiService } from '@core/services/api.service';
import { ExerciseSelectorComponent } from './exercise-selector.component';

describe('ExerciseSelectorComponent', () => {
  const api = { listExercises: jasmine.createSpy('listExercises') };
  beforeEach(() => {
    api.listExercises.calls.reset();
    api.listExercises.and.returnValue(of([]));
    TestBed.configureTestingModule({
      imports: [ExerciseSelectorComponent, NoopAnimationsModule],
      providers: [provideRouter([]), { provide: ApiService, useValue: api }],
    });
  });

  it('does not present failed requests as an empty catalogue and supports retry', () => {
    api.listExercises.and.returnValue(throwError(() => new Error('Unavailable')));
    const fixture = TestBed.createComponent(ExerciseSelectorComponent);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[role="alert"]')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('tn-empty-state')).toBeNull();
    api.listExercises.and.returnValue(of([]));
    fixture.componentInstance.load();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[role="alert"]')).toBeNull();
    expect(fixture.nativeElement.querySelector('tn-empty-state')).toBeTruthy();
  });

  it('links to the actual exercise ID rather than using select as an ID', () => {
    api.listExercises.and.returnValue(of([{ id: 'exercise-123', name: 'Test exercise', state: 'running' }]));
    const fixture = TestBed.createComponent(ExerciseSelectorComponent);
    fixture.detectChanges();
    const hrefs = [...fixture.nativeElement.querySelectorAll('a')].map(a => (a as HTMLAnchorElement).getAttribute('href'));
    expect(hrefs).toContain('/ops-center/exercise-123');
    expect(hrefs).toContain('/exercises/exercise-123');
    expect(api.listExercises).toHaveBeenCalledWith(25, 0);
    fixture.componentInstance.page(1);
    expect(api.listExercises).toHaveBeenCalledWith(25, 25);
  });
});
