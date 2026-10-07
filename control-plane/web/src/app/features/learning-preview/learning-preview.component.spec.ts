import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';

import { LearningPreviewComponent } from './learning-preview.component';

describe('LearningPreviewComponent', () => {
  let harness: RouterTestingHarness;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [NoopAnimationsModule],
      providers: [provideRouter([{ path: 'preview', component: LearningPreviewComponent }])],
    }).compileComponents();
    harness = await RouterTestingHarness.create();
  });

  async function open(url: string): Promise<{ cmp: LearningPreviewComponent; el: HTMLElement }> {
    const cmp = await harness.navigateByUrl(url, LearningPreviewComponent);
    harness.detectChanges();
    return { cmp, el: harness.routeNativeElement as HTMLElement };
  }

  const current = (el: HTMLElement) =>
    Array.from(el.querySelectorAll('nav a[aria-current="page"]')).map(a => a.textContent?.trim());

  it('creates and defaults to the LMS development preview', async () => {
    const { cmp, el } = await open('/preview');
    expect(cmp).toBeTruthy();
    expect(cmp.view()).toBe('development');
    const frame = el.querySelector('iframe')!;
    expect(frame.getAttribute('src')).toBe('assets/previews/development.html');
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts');
    expect(current(el)).toEqual(['LMS development']);
    expect(el.querySelector('.notice')).not.toBeNull();
  });

  it('shows the commander readiness preview for ?view=readiness', async () => {
    const { cmp, el } = await open('/preview?view=readiness');
    expect(cmp.view()).toBe('readiness');
    expect(el.querySelector('iframe')!.getAttribute('src')).toBe('assets/previews/readiness.html');
    expect(current(el)).toEqual(['Commander readiness']);
  });

  it('shows the Moodle panel with no iframe for ?view=moodle', async () => {
    const { cmp, el } = await open('/preview?view=moodle');
    expect(cmp.view()).toBe('moodle');
    expect(el.querySelector('iframe')).toBeNull();
    expect(el.querySelector('article h2')?.textContent).toContain('Moodle test LMS');
    const external = el.querySelector<HTMLAnchorElement>('a[target="_blank"]')!;
    expect(external.getAttribute('rel')).toContain('noopener');
    expect(current(el)).toEqual(['Moodle']);
  });

  it('falls back to development for an unknown view', async () => {
    const { cmp, el } = await open('/preview?view=bogus');
    expect(cmp.view()).toBe('development');
    expect(el.querySelector('iframe')!.getAttribute('src')).toBe('assets/previews/development.html');
  });

  it('switches view when the query param changes on the same route', async () => {
    const { cmp } = await open('/preview?view=moodle');
    expect(cmp.view()).toBe('moodle');
    const again = await harness.navigateByUrl('/preview?view=readiness', LearningPreviewComponent);
    expect(again).toBe(cmp);
    expect(cmp.view()).toBe('readiness');
  });

  it('navigates via the workspace links', async () => {
    const { cmp, el } = await open('/preview');
    const moodleLink = Array.from(el.querySelectorAll<HTMLAnchorElement>('nav a'))
      .find(a => a.textContent?.includes('Moodle'))!;
    expect(moodleLink.getAttribute('href')).toBe('/preview?view=moodle');
    moodleLink.click();
    await harness.fixture.whenStable();
    harness.detectChanges();
    expect(cmp.view()).toBe('moodle');
  });
});
