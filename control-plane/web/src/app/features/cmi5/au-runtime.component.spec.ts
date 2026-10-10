import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute, Router, convertToParamMap } from '@angular/router';
import { of } from 'rxjs';

import { Cmi5ApiService, Cmi5Content } from '@core/services/cmi5-api.service';
import { Cmi5AuRuntimeComponent, defaultAuFactory } from './au-runtime.component';
import { Cmi5Au } from './cmi5-au';

const LAUNCH = {
  endpoint: '/api/cmi5/lrs/', fetch: '/api/cmi5/fetch/s', actor: '{"account":{"homePage":"h","name":"n"}}',
  registration: 'r', activityId: 'a',
};

function content(): Cmi5Content {
  return {
    index: 0, title: 'Module 1', lang: 'en-CA', move_on: 'Passed', mastery_score: 0.7,
    pages: [{ path: 'p1', html: '<p>One</p>' }, { path: 'p2', html: '<p>Two</p>' }],
    quiz: { title: 'Quiz 1', questions: [{ id: 'q1', stem: 'Which?', options: ['x', 'y'] }] },
  } as Cmi5Content;
}

function fakeAu(mode = 'Normal'): jasmine.SpyObj<Cmi5Au> {
  const au = jasmine.createSpyObj<Cmi5Au>('Cmi5Au', ['start', 'progress', 'complete', 'score', 'terminate']);
  au.start.and.callFake(async () => au);
  au.progress.and.resolveTo(null);
  au.complete.and.resolveTo(null);
  au.score.and.resolveTo(null);
  au.terminate.and.callFake(async opts => {
    (au as any).terminated = true;
    opts?.redirect?.('https://lms.example/back');
    return null;
  });
  Object.assign(au, { terminated: false, launchData: { returnURL: 'https://lms.example/back' } });
  Object.defineProperty(au, 'mode', { get: () => mode });
  Object.defineProperty(au, 'judged', { get: () => mode === 'Normal' });
  Object.defineProperty(au, 'masteryScore', { get: () => 0.7 });
  return au;
}

describe('Cmi5AuRuntimeComponent', () => {
  let fixture: ComponentFixture<Cmi5AuRuntimeComponent>;
  let api: jasmine.SpyObj<Cmi5ApiService>;
  let router: jasmine.SpyObj<Router>;
  let au: jasmine.SpyObj<Cmi5Au>;
  let left: string[];

  async function setUp(query: Record<string, string>, mode = 'Normal'): Promise<void> {
    api = jasmine.createSpyObj('Cmi5ApiService', ['content', 'grade']);
    api.content.and.returnValue(of(content()));
    api.grade.and.returnValue(of({ correct: 1, total: 1, scaled: 1 }));
    router = jasmine.createSpyObj('Router', ['navigate']);
    router.navigate.and.resolveTo(true);
    au = fakeAu(mode);
    Cmi5AuRuntimeComponent.auFactory = () => au;
    left = [];
    Cmi5AuRuntimeComponent.leave = url => left.push(url);
    TestBed.configureTestingModule({
      imports: [Cmi5AuRuntimeComponent, NoopAnimationsModule],
      providers: [
        { provide: Cmi5ApiService, useValue: api },
        { provide: Router, useValue: router },
        {
          provide: ActivatedRoute,
          useValue: {
            snapshot: {
              paramMap: convertToParamMap({ releaseId: 'rel-1', auIndex: '0' }),
              queryParamMap: convertToParamMap(query),
            },
          },
        },
      ],
    });
    fixture = TestBed.createComponent(Cmi5AuRuntimeComponent);
    fixture.detectChanges();
    await fixture.whenStable();
    await settle();
  }

  async function settle(): Promise<void> {
    for (let i = 0; i < 5; i++) {
      await new Promise(r => setTimeout(r));
      fixture.detectChanges();
    }
  }

  const text = () => (fixture.nativeElement as HTMLElement).textContent ?? '';
  const click = async (id: string) => {
    (fixture.nativeElement.querySelector(`[data-testid="${id}"]`) as HTMLElement).click();
    await settle();
  };

  afterEach(() => {
    fixture?.destroy();
    Cmi5AuRuntimeComponent.auFactory = defaultAuFactory;
  });

  it('starts the cmi5 session, then shows the first page and reports progress', async () => {
    await setUp(LAUNCH);
    expect(au.start).toHaveBeenCalled();
    expect(api.content).toHaveBeenCalledWith('rel-1', 0);
    expect(text()).toContain('Tracked session (Normal). Pass mark: 70%.');
    expect(text()).toContain('One');
    expect(au.progress).toHaveBeenCalledWith(50);
  });

  it('completes after the last page, then marks the quiz on the server and reports the score', async () => {
    await setUp(LAUNCH);
    await click('au-next');
    expect(au.progress).toHaveBeenCalledWith(100);
    await click('au-next'); // Finish reading
    expect(au.complete).toHaveBeenCalled();
    expect(text()).toContain('Quiz 1');
    fixture.componentInstance.answer('q1', 'B');
    await click('au-submit');
    expect(api.grade).toHaveBeenCalledWith('rel-1', 0, { q1: 'B' });
    expect(au.score).toHaveBeenCalledWith(1, { raw: 1, min: 0, max: 1 }, 0.7);
    expect(text()).toContain('1 / 1 correct (100%). Passed.');
  });

  it('terminates on Exit and returns to the LMS', async () => {
    await setUp(LAUNCH);
    await click('au-exit');
    expect(au.terminate).toHaveBeenCalled();
    expect(left).toEqual(['https://lms.example/back']);
    expect(router.navigate).not.toHaveBeenCalled();
  });

  it('judges nothing in Review mode', async () => {
    await setUp(LAUNCH, 'Review');
    await click('au-next');
    await click('au-next');
    expect(au.complete).not.toHaveBeenCalled();
    fixture.componentInstance.answer('q1', 'A');
    await click('au-submit');
    expect(au.score).not.toHaveBeenCalled();
  });

  it('without a launch it is an untracked preview', async () => {
    await setUp({});
    expect(au.start).not.toHaveBeenCalled();
    expect(text()).toContain('Preview: not tracked');
    expect(text()).toContain('One');
    await click('au-exit');
    expect(router.navigate).toHaveBeenCalledWith(['/au/releases', 'rel-1']);
  });

  it('says what went wrong when the session cannot start', async () => {
    api = jasmine.createSpyObj('Cmi5ApiService', ['content', 'grade']);
    const failing = fakeAu();
    failing.start.and.rejectWith(new Error('fetch URL error 1: used'));
    Cmi5AuRuntimeComponent.auFactory = () => failing;
    TestBed.configureTestingModule({
      imports: [Cmi5AuRuntimeComponent, NoopAnimationsModule],
      providers: [
        { provide: Cmi5ApiService, useValue: api },
        { provide: Router, useValue: jasmine.createSpyObj('Router', ['navigate']) },
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { paramMap: convertToParamMap({ releaseId: 'r', auIndex: '0' }), queryParamMap: convertToParamMap(LAUNCH) } },
        },
      ],
    });
    fixture = TestBed.createComponent(Cmi5AuRuntimeComponent);
    fixture.detectChanges();
    await settle();
    expect(text()).toContain('fetch URL error 1: used');
    expect(api.content).not.toHaveBeenCalled();
  });
});
