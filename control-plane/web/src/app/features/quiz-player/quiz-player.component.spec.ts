import { Component, Input } from '@angular/core';
import { ComponentFixture, TestBed, fakeAsync, tick, discardPeriodicTasks } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { MatSnackBar } from '@angular/material/snack-bar';
import { of, throwError } from 'rxjs';

import { ApiService } from '@core/services/api.service';
import { MotionService } from '../../shared/motion';
import { LottieIconComponent } from '../../shared/components/lottie-icon.component';
import { QuizPlayerComponent } from './quiz-player.component';

@Component({ selector: 'tn-lottie', template: '' })
class LottieStubComponent {
  @Input() name = '';
  @Input() size = 0;
}

describe('QuizPlayerComponent', () => {
  let fixture: ComponentFixture<QuizPlayerComponent>;
  let component: QuizPlayerComponent;
  let el: HTMLElement;
  let api: jasmine.SpyObj<ApiService>;
  let snackOpen: jasmine.Spy;

  const QUIZ = { id: 'q1', title: 'Network Basics', description: 'Ports and protocols', question_count: 2, pass_pct: 70 };
  const ATTEMPT = {
    attempt_id: 'att-1',
    title: 'Network Basics',
    time_limit_minutes: null,
    questions: [
      { id: 'qa', question_type: 'mcq', points: 1, stem: 'Which port is SSH?', options: ['21', '22', '80'] },
      { id: 'qb', question_type: 'multi', points: 2, stem: 'Which are UDP?', options: ['DNS', 'HTTP', 'NTP', 'SMTP'] },
    ],
  };
  const RESULT = {
    passed: true, pct: 100, score: 3, max_score: 3,
    results: [
      { question_id: 'qa', correct: true, points_earned: 1, points_possible: 1, explanation: 'SSH is 22/tcp' },
      { question_id: 'qb', correct: true, points_earned: 2, points_possible: 2, explanation: null },
    ],
  };

  let query: Record<string, string>;

  beforeEach(async () => {
    query = { quiz: 'q1' };
    api = jasmine.createSpyObj('ApiService', ['getQuiz', 'startQuizAttempt', 'submitQuizAttempt']);
    api.getQuiz.and.returnValue(of(QUIZ));
    api.startQuizAttempt.and.returnValue(of(ATTEMPT));
    api.submitQuizAttempt.and.returnValue(of(RESULT));

    await TestBed.configureTestingModule({
      imports: [QuizPlayerComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: ApiService, useValue: api },
        // Factory, so each test can set `query` before the component is created.
        { provide: ActivatedRoute, useFactory: () => ({ snapshot: { queryParamMap: convertToParamMap(query) } }) },
        { provide: MotionService, useValue: { reducedMotion: () => true, runOutside: (fn: () => unknown) => fn() } },
      ],
    })
      .overrideComponent(QuizPlayerComponent, {
        remove: { imports: [LottieIconComponent] },
        add: { imports: [LottieStubComponent] },
      })
      .compileComponents();
  });

  function setup(): void {
    fixture = TestBed.createComponent(QuizPlayerComponent);
    component = fixture.componentInstance;
    snackOpen = spyOn(fixture.debugElement.injector.get(MatSnackBar), 'open');
    el = fixture.nativeElement;
    fixture.detectChanges();
  }

  const buttons = () => Array.from(el.querySelectorAll<HTMLButtonElement>('button'));
  const button = (text: string) => buttons().find(b => b.textContent?.includes(text))!;
  const options = () => Array.from(el.querySelectorAll<HTMLButtonElement>('button.option'));
  const click = (b: HTMLElement) => { b.click(); fixture.detectChanges(); };

  it('creates and loads the quiz named in ?quiz=', () => {
    setup();
    expect(component).toBeTruthy();
    expect(api.getQuiz).toHaveBeenCalledWith('q1');
    expect(el.querySelector('h1')?.textContent).toContain('Network Basics');
    expect(el.textContent).toContain('2 questions');
    expect(el.textContent).toContain('pass 70%');
  });

  it('does not request a quiz when no id is given', () => {
    query = {};
    setup();
    expect(api.getQuiz).not.toHaveBeenCalled();
    expect(el.querySelector('h1')?.textContent).toContain('Quiz');
  });

  it('shows a snackbar when the quiz cannot be loaded', () => {
    query = { quiz: 'missing' };
    api.getQuiz.and.returnValue(throwError(() => ({ status: 404 })));
    setup();
    expect(api.getQuiz).toHaveBeenCalledWith('missing');
    expect(snackOpen).toHaveBeenCalledWith('Quiz not found', 'OK', jasmine.any(Object));
    expect(el.querySelector('.intro-card')).not.toBeNull();
  });

  it('starts an attempt and renders the first question', () => {
    setup();
    click(button('Start attempt'));
    expect(api.startQuizAttempt).toHaveBeenCalledWith('q1');
    expect(el.querySelector('.intro-card')).toBeNull();
    expect(el.querySelector('.q-stem')?.textContent).toContain('Which port is SSH?');
    expect(el.querySelector('.progress-label')?.textContent?.trim()).toBe('1 / 2');
    expect(el.querySelector('.timer')).toBeNull();
    expect(options().every(o => o.getAttribute('role') === 'radio')).toBeTrue();
  });

  it('surfaces err.error.detail when starting fails', () => {
    setup();
    api.startQuizAttempt.and.returnValue(throwError(() => ({ error: { detail: 'No attempts remaining' } })));
    click(button('Start attempt'));
    expect(snackOpen).toHaveBeenCalledWith('No attempts remaining', 'OK', jasmine.any(Object));
    expect(component.starting()).toBeFalse();
    expect(component.attempt()).toBeNull();
    expect(el.querySelector('.intro-card')).not.toBeNull();
  });

  it('uses a generic message when start fails without a detail', () => {
    setup();
    api.startQuizAttempt.and.returnValue(throwError(() => ({ error: null })));
    component.start();
    expect(snackOpen).toHaveBeenCalledWith('Could not start attempt', 'OK', jasmine.any(Object));
  });

  it('selects single and multiple answers, then submits the answer map and shows the result', () => {
    setup();
    click(button('Start attempt'));

    // Single choice: the last click wins.
    click(options()[0]);
    click(options()[1]);
    expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'true', 'false']);

    click(button('Next'));
    expect(el.querySelector('.q-stem')?.textContent).toContain('Which are UDP?');
    expect(options().every(o => o.getAttribute('role') === 'checkbox')).toBeTrue();

    // Multiple answers: toggles accumulate, a second click clears.
    click(options()[2]);
    click(options()[0]);
    click(options()[1]);
    click(options()[1]);
    expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['true', 'false', 'true', 'false']);
    expect(button('Submit').textContent).toContain('2/2 answered');

    // Back keeps the earlier answer.
    click(button('Back'));
    expect(options()[1].getAttribute('aria-checked')).toBe('true');
    click(button('Next'));

    click(button('Submit'));
    expect(api.submitQuizAttempt).toHaveBeenCalledWith('att-1', { qa: [1], qb: [0, 2] });
    expect(component.attempt()).toBeNull();
    expect(el.querySelector('.result-card h1')?.textContent).toContain('Passed!');
    expect(el.querySelector('.score')?.textContent).toContain('100%');
    expect(el.querySelectorAll('.review-row.ok').length).toBe(2);
    expect(el.textContent).toContain('SSH is 22/tcp');
    expect(el.querySelector('tn-lottie')).not.toBeNull();
  });

  it('shows "Not yet" for a failed result', () => {
    setup();
    api.submitQuizAttempt.and.returnValue(of({
      ...RESULT, passed: false, pct: 33, score: 1,
      results: [{ ...RESULT.results[0] }, { ...RESULT.results[1], correct: false, points_earned: 0 }],
    }));
    component.start();
    component.submit();
    fixture.detectChanges();
    expect(el.querySelector('.result-card h1')?.textContent).toContain('Not yet');
    expect(el.querySelectorAll('.review-row.bad').length).toBe(1);
    expect(el.querySelector('tn-lottie')).toBeNull();
  });

  it('keeps the attempt open and shows the detail when submit fails', () => {
    setup();
    api.submitQuizAttempt.and.returnValue(throwError(() => ({ error: { detail: 'Attempt expired' } })));
    component.start();
    component.selectSingle('qa', 0);
    component.submit();
    fixture.detectChanges();
    expect(snackOpen).toHaveBeenCalledWith('Attempt expired', 'OK', jasmine.any(Object));
    expect(component.submitting()).toBeFalse();
    expect(component.attempt()).not.toBeNull();
    expect(component.result()).toBeNull();
  });

  it('retake clears the result and starts a fresh attempt', () => {
    setup();
    component.start();
    component.selectSingle('qa', 2);
    component.submit();
    fixture.detectChanges();
    click(button('Retake'));
    expect(api.startQuizAttempt).toHaveBeenCalledTimes(2);
    expect(component.result()).toBeNull();
    expect(component.answers).toEqual({});
    expect(component.index()).toBe(0);
    expect(el.querySelector('.q-stem')?.textContent).toContain('Which port is SSH?');
  });

  it('counts down a timed attempt and auto-submits when time runs out', fakeAsync(() => {
    setup();
    api.startQuizAttempt.and.returnValue(of({ ...ATTEMPT, time_limit_minutes: 1 }));
    component.start();
    fixture.detectChanges();
    expect(el.querySelector('.timer')?.textContent).toContain('1:00');

    tick(1000);
    fixture.detectChanges();
    expect(el.querySelector('.timer')?.textContent).toContain('0:59');
    expect(el.querySelector('.timer')?.classList).toContain('low');

    component.selectSingle('qa', 1);
    tick(59_000);
    fixture.detectChanges();
    expect(snackOpen).toHaveBeenCalledWith('Time is up — submitting', '', jasmine.any(Object));
    expect(api.submitQuizAttempt).toHaveBeenCalledOnceWith('att-1', { qa: [1] });
    expect(el.querySelector('.result-card')).not.toBeNull();

    // The interval was cleared; no further submits happen.
    tick(5000);
    expect(api.submitQuizAttempt).toHaveBeenCalledTimes(1);
    discardPeriodicTasks();
  }));
});
