import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { of, throwError } from 'rxjs';

import { Cmi5ApiService, Cmi5Structure } from '@core/services/cmi5-api.service';
import { Cmi5LauncherComponent } from './au-launcher.component';

function structure(enrolled = true): Cmi5Structure {
  const au = (index: number, extra = {}) => ({
    index, publisher_id: `p${index}`, title: `Module ${index + 1}`, description: '', move_on: 'Passed',
    mastery_score: 0.7, url: `https://tn/au/releases/rel-1/${index}`, completed: false, passed: false,
    waived: null, satisfied: false, ...extra,
  });
  return {
    release_id: 'rel-1', course_id: 'c-1', publisher_id: 'p', title: 'Foundations', registration: 'reg-1',
    course_satisfied: false, enrolled,
    aus: [au(0, { completed: true, passed: true, satisfied: true }), au(1), au(2, { waived: 'Tested Out', satisfied: true })],
  } as Cmi5Structure;
}

describe('Cmi5LauncherComponent', () => {
  let fixture: ComponentFixture<Cmi5LauncherComponent>;
  let api: jasmine.SpyObj<Cmi5ApiService>;
  let opened: string[];

  function setUp(s: Cmi5Structure): void {
    api = jasmine.createSpyObj('Cmi5ApiService', ['structure', 'launch']);
    api.structure.and.returnValue(of(s));
    opened = [];
    Cmi5LauncherComponent.open = url => opened.push(url);
    TestBed.configureTestingModule({
      imports: [Cmi5LauncherComponent, NoopAnimationsModule],
      providers: [
        { provide: Cmi5ApiService, useValue: api },
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({ releaseId: 'rel-1' }) } } },
      ],
    });
    fixture = TestBed.createComponent(Cmi5LauncherComponent);
    fixture.detectChanges();
  }

  const el = (id: string) => fixture.nativeElement.querySelector(`[data-testid="${id}"]`) as HTMLElement;

  afterEach(() => fixture?.destroy());

  it('lists the modules with what each needs and where the Student stands', () => {
    setUp(structure());
    expect(el('au-0').textContent).toContain('Pass the quiz (70%) · Done');
    expect(el('au-1').textContent).toContain('Not started');
    expect(el('au-2').textContent).toContain('Waived (Tested Out)');
    expect(el('launch-0').textContent).toContain('Review');
    expect(el('launch-1').textContent).toContain('Launch');
  });

  it('launches through TrueNorth and opens the AU in this window', () => {
    setUp(structure());
    api.launch.and.returnValue(of({ url: 'https://tn/au/releases/rel-1/1?endpoint=x', session_id: 's', registration: 'r', launch_mode: 'Normal', launch_method: 'OwnWindow' }));
    el('launch-1').click();
    expect(api.launch).toHaveBeenCalledWith('rel-1', 1);
    expect(opened).toEqual(['https://tn/au/releases/rel-1/1?endpoint=x']);
  });

  it('shows why a launch was refused', () => {
    setUp(structure());
    api.launch.and.returnValue(throwError(() => ({ error: { detail: 'the LRS is unavailable' } })));
    el('launch-1').click();
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('the LRS is unavailable');
    expect(opened).toEqual([]);
  });

  it('offers no launch to someone not enrolled on this release', () => {
    setUp(structure(false));
    expect(el('launch-0')).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('not enrolled on this version');
  });
});
