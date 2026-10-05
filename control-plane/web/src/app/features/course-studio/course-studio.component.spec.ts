import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatDialog } from '@angular/material/dialog';
import { of, throwError } from 'rxjs';

import { CourseStudioComponent } from './course-studio.component';
import { CoursePublication, CourseRelease, CourseStudioApiService, LearningPlatform } from '@core/services/course-studio-api.service';
import { NotificationService } from '@core/services/notification.service';

function release(over: Partial<CourseRelease> = {}): CourseRelease {
  return {
    id: 'r1', course_id: 'c1', catalogue_code: 'C105', arc2_code: 'ARC2-C105', title: 'Foundations of Cybersecurity',
    version: 1, state: 'candidate', release_digest: 'd', learner_digest: 'l', platform_digest: 'p',
    instructor_digest: 'i', activities: { mod_001: 'theory', mod_002: 'theory', mod_003: 'range' },
    open_actions: [{ id: 'po.x', category: 'standards', text: 'Standards decides the PO binding' }],
    acknowledged_actions: [], created_at: null, accepted_at: null, accepted_by: null, notes: '',
    ...over,
  } as CourseRelease;
}

describe('CourseStudioComponent', () => {
  let fixture: ComponentFixture<CourseStudioComponent>;
  let api: jasmine.SpyObj<CourseStudioApiService>;
  let notify: jasmine.SpyObj<NotificationService>;
  let dialogResult: unknown;

  function setUp(releases: CourseRelease[]): void {
    api = jasmine.createSpyObj('CourseStudioApiService', [
      'releases', 'platforms', 'publications', 'upload', 'accept', 'publish', 'retryPublication', 'instructorBundle',
    ]);
    notify = jasmine.createSpyObj('NotificationService', ['success', 'error', 'info']);
    api.releases.and.returnValue(of(releases));
    api.platforms.and.returnValue(of([{ id: 'p1', name: 'Tenant Moodle', platform_type: 'moodle', is_active: true } as unknown as LearningPlatform]));
    api.publications.and.returnValue(of([]));
    api.accept.and.returnValue(of(release({ state: 'accepted' })));
    api.publish.and.returnValue(of({ id: 'pub1', release_id: 'r1', state: 'requested' } as unknown as CoursePublication));
    TestBed.configureTestingModule({
      imports: [CourseStudioComponent, NoopAnimationsModule],
      providers: [
        { provide: CourseStudioApiService, useValue: api },
        { provide: NotificationService, useValue: notify },
        { provide: MatDialog, useValue: { open: () => ({ afterClosed: () => of(dialogResult) }) } },
      ],
    });
    fixture = TestBed.createComponent(CourseStudioComponent);
    fixture.detectChanges();
  }

  it('lists releases with their activity mix and open actions', () => {
    setUp([release()]);
    const text = fixture.nativeElement.textContent as string;
    expect(text).toContain('C105');
    expect(text).toContain('2 theory · 1 range');
    expect(text).toContain('1 open action(s)');
  });

  it('accepts a candidate with the acknowledgements from the dialog', () => {
    setUp([release()]);
    dialogResult = { acknowledge_actions: ['po.x'], notes: '' };
    fixture.componentInstance.accept(release());
    expect(api.accept).toHaveBeenCalledWith('r1', { acknowledge_actions: ['po.x'], notes: '' });
    expect(notify.success).toHaveBeenCalled();
  });

  it('publishes an accepted release to the chosen Moodle', () => {
    setUp([release({ state: 'accepted' })]);
    dialogResult = 'p1';
    fixture.componentInstance.publish(release({ state: 'accepted' }));
    expect(api.publish).toHaveBeenCalledWith('r1', 'p1');
  });

  it('shows the API refusal when a load fails', () => {
    setUp([]);
    api.releases.and.returnValue(throwError(() => ({ status: 500 })));
    fixture.componentInstance.load();
    expect(notify.error).toHaveBeenCalledWith('Could not load course releases');
  });
});
