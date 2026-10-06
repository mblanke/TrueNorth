import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute, Router, convertToParamMap } from '@angular/router';
import { of } from 'rxjs';

import { LabPageComponent } from './lab-page.component';
import { CourseStudioApiService, LabSession } from '@core/services/course-studio-api.service';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';

function lab(state: string): LabSession {
  return {
    id: 's1', state, release_id: 'r1', activity_id: 'mod_006', attempt: 1, error: '', probes: [], evidence_count: 0,
    readiness_seconds: null, created_at: null, ready_at: null, idle_expires_at: null, max_expires_at: null,
    ended_at: null, end_reason: '',
  } as LabSession;
}

describe('LabPageComponent', () => {
  let fixture: ComponentFixture<LabPageComponent>;
  let api: jasmine.SpyObj<CourseStudioApiService>;
  let router: jasmine.SpyObj<Router>;

  function setUp(fragment: string | null, authenticated: boolean, state = 'provisioning'): void {
    try { sessionStorage.removeItem('tn-lab-token:s1'); } catch { /* storage may be unavailable */ }
    api = jasmine.createSpyObj('CourseStudioApiService', ['lab', 'labAction', 'labConsole']);
    api.lab.and.returnValue(of(lab(state)));
    api.labAction.and.returnValue(of(lab('active')));
    api.labConsole.and.returnValue(of({ node: 'host', kind: 'mock', url: 'mock://console/vm', expires_in: 60 }));
    router = jasmine.createSpyObj('Router', ['navigate']);
    router.navigate.and.returnValue(Promise.resolve(true));
    TestBed.configureTestingModule({
      imports: [LabPageComponent, NoopAnimationsModule],
      providers: [
        { provide: CourseStudioApiService, useValue: api },
        { provide: Router, useValue: router },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['error']) },
        { provide: AuthService, useValue: { bootstrap: () => Promise.resolve(null), isAuthenticated: () => authenticated } },
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({ id: 's1' }), fragment } } },
      ],
    });
    fixture = TestBed.createComponent(LabPageComponent);
    fixture.detectChanges();
  }

  afterEach(() => fixture?.destroy());

  it('uses the launch token from the fragment and says what is happening', () => {
    setUp('token=abc', false);
    expect(api.lab).toHaveBeenCalledWith('s1', 'abc');
    expect(fixture.nativeElement.textContent).toContain('Building your lab');
  });

  it('sends someone with neither token nor session to sign in', fakeAsync(() => {
    setUp(null, false);
    tick();
    expect(router.navigate).toHaveBeenCalledWith(['/login'], { queryParams: { returnUrl: '/labs/s1' } });
    expect(api.lab).not.toHaveBeenCalled();
  }));

  it('opens a console on a ready lab', () => {
    setUp('token=abc', false, 'ready');
    fixture.componentInstance.openConsole();
    fixture.detectChanges();
    expect(api.labConsole).toHaveBeenCalledWith('s1', 'abc');
    expect(fixture.nativeElement.textContent).toContain('mock://console/vm');
  });

  it('asks before ending the lab', () => {
    setUp('token=abc', false, 'active');
    spyOn(window, 'confirm').and.returnValue(false);
    fixture.componentInstance.act('end');
    expect(api.labAction).not.toHaveBeenCalled();
  });
});
