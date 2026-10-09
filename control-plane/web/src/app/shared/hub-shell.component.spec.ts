import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { ActivatedRoute, provideRouter } from '@angular/router';
import { environment } from '@env/environment';
import { AuthService } from '@core/services/auth.service';
import { HubShellComponent } from './hub-shell.component';

const STATUS = `${environment.apiUrl}/arc2/status`;
const AUTHORING = {
  title: 'Authoring Studio',
  tabs: [{ label: 'Course Studio', path: 'studio', requires: 'arc2' }, { label: 'Ranges', path: 'ranges' }],
};

describe('HubShellComponent', () => {
  let http: HttpTestingController;

  function render(data: object): HTMLElement {
    TestBed.configureTestingModule({
      imports: [HubShellComponent],
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([]),
        { provide: ActivatedRoute, useValue: { snapshot: { data } } },
        { provide: AuthService, useValue: { isInstructor: signal(true) } }],
    });
    http = TestBed.inject(HttpTestingController);
    const fixture = TestBed.createComponent(HubShellComponent);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  const labels = (el: HTMLElement) => Array.from(el.querySelectorAll('.hub-tabs a')).map(a => a.textContent!.trim());

  it('hides the ARC² Course Studio tab when the server has it off', () => {
    const el = render(AUTHORING);
    expect(labels(el)).toEqual(['Ranges']);  // not shown while unknown either
    http.expectOne(STATUS).flush({ enabled: false, reason: 'ARC² Course Studio is not enabled on this server.' });
    TestBed.tick();
    expect(labels(el)).toEqual(['Ranges']);
    http.verify();
  });

  it('shows it when the server has it on', () => {
    const el = render(AUTHORING);
    http.expectOne(STATUS).flush({ enabled: true, reason: null });
    TestBed.tick();
    expect(labels(el)).toEqual(['Course Studio', 'Ranges']);
  });

  it('hides it if the check fails', () => {
    const el = render(AUTHORING);
    http.expectOne(STATUS).flush({ detail: 'Bad gateway' }, { status: 502, statusText: 'Bad Gateway' });
    TestBed.tick();
    expect(labels(el)).toEqual(['Ranges']);
  });

  it('does not ask about ARC² in a hub without that tab', () => {
    const el = render({ title: 'Learning', tabs: [{ label: 'Courses', path: 'courses' }] });
    expect(labels(el)).toEqual(['Courses']);
    http.expectNone(STATUS);
    http.verify();
  });
});
