import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { RangeCredentialsApiService, TemplateIssue } from '@core/services/range-credentials-api.service';
import { TemplateIssuesBannerComponent } from './template-issues-banner.component';

const ISSUE: TemplateIssue = {
  template_id: 't1', name: 'Legacy lab', version: '1.0', tenant_id: 'tn1', upcoming_bookings: 2,
  problems: ['accounts.0.password: a typed-in password is allowed only on a scenario account'],
};

describe('TemplateIssuesBannerComponent', () => {
  async function render(answer: TemplateIssue[] | HttpErrorResponse) {
    const api = jasmine.createSpyObj('RangeCredentialsApiService', ['templateIssues']);
    api.templateIssues.and.returnValue(answer instanceof HttpErrorResponse ? throwError(() => answer) : of(answer));
    await TestBed.configureTestingModule({
      imports: [TemplateIssuesBannerComponent],
      providers: [{ provide: RangeCredentialsApiService, useValue: api }],
    }).compileComponents();
    const fixture = TestBed.createComponent(TemplateIssuesBannerComponent);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('warns of templates that cannot be provisioned, with the bookings that would fail', async () => {
    const el = await render([ISSUE]);
    expect(el.querySelector('[role="alert"]')).toBeTruthy();
    expect(el.textContent).toContain('Legacy lab');
    expect(el.textContent).toContain('2 upcoming booking(s) will fail');
    expect(el.textContent).toContain('scenario: true');
  });

  it('shows nothing when there is nothing to fix, or the role may not see it', async () => {
    expect((await render([])).querySelector('[role="alert"]')).toBeNull();
    TestBed.resetTestingModule();
    expect((await render(new HttpErrorResponse({ status: 403 }))).querySelector('[role="alert"]')).toBeNull();
  });
});
