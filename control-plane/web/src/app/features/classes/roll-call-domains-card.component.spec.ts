import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';
import { ClassesApiService, RollCallDomains } from '@core/services/classes-api.service';
import { RollCallDomainsCardComponent, domainsSummary, parseDomains } from './roll-call-domains-card.component';

describe('parseDomains / domainsSummary (#318)', () => {
  it('reads domains one per line or comma separated, without @ or case, once each', () => {
    expect(parseDomains(' Forces.GC.ca\n@dnd.ca, forces.gc.ca ;x.org ')).toEqual(['forces.gc.ca', 'dnd.ca', 'x.org']);
    expect(parseDomains('  \n ')).toEqual([]);
  });

  it('says what each setting means', () => {
    expect(domainsSummary({ email_domains: ['forces.gc.ca'], source: 'setting' })).toContain('forces.gc.ca');
    expect(domainsSummary({ email_domains: [], source: 'setting' })).toContain('any domain');
    expect(domainsSummary({ email_domains: ['corp.example'], source: 'accounts' })).toContain('AD accounts (corp.example)');
    expect(domainsSummary({ email_domains: [], source: 'accounts' })).toContain('every roll-call add is refused');
  });
});

describe('RollCallDomainsCardComponent (#318)', () => {
  let fixture: ComponentFixture<RollCallDomainsCardComponent>;
  let api: jasmine.SpyObj<ClassesApiService>;
  const el = () => fixture.nativeElement as HTMLElement;
  const button = (label: string) =>
    Array.from(el().querySelectorAll('button')).find(b => b.textContent?.includes(label)) as HTMLButtonElement | undefined;

  function render(current: RollCallDomains, canEdit: boolean) {
    api = jasmine.createSpyObj('ClassesApiService', ['rollCallDomains', 'setRollCallDomains']);
    api.rollCallDomains.and.returnValue(of(current));
    TestBed.configureTestingModule({
      imports: [RollCallDomainsCardComponent, NoopAnimationsModule],
      providers: [{ provide: ClassesApiService, useValue: api }],
    });
    fixture = TestBed.createComponent(RollCallDomainsCardComponent);
    fixture.componentRef.setInput('canEdit', canEdit);
    fixture.detectChanges();
  }

  it('shows an instructor the setting, with nothing to change', () => {
    render({ email_domains: [], source: 'accounts' }, false);
    expect(el().textContent).toContain('Roll-call domains');
    expect(el().textContent).toContain('every roll-call add is refused');
    expect(button('Change')).toBeUndefined();
  });

  it('lets an admin set the domains', () => {
    render({ email_domains: [], source: 'accounts' }, true);
    api.setRollCallDomains.and.returnValue(of({ email_domains: ['forces.gc.ca', 'dnd.ca'], source: 'setting' }));
    button('Change')!.click();
    fixture.detectChanges();
    fixture.componentInstance.text.set('forces.gc.ca, @DND.ca');
    button('Save')!.click();
    fixture.detectChanges();
    expect(api.setRollCallDomains).toHaveBeenCalledWith(['forces.gc.ca', 'dnd.ca']);
    expect(el().textContent).toContain('Roll calls accept addresses at forces.gc.ca, dnd.ca.');
    expect(button('Save')).toBeUndefined();
  });

  it('lets an admin allow any domain', () => {
    render({ email_domains: ['forces.gc.ca'], source: 'setting' }, true);
    api.setRollCallDomains.and.returnValue(of({ email_domains: [], source: 'setting' }));
    button('Change')!.click();
    fixture.detectChanges();
    fixture.componentInstance.anyDomain.set(true);
    fixture.componentInstance.save();
    expect(api.setRollCallDomains).toHaveBeenCalledWith([]);
  });

  it('refuses an empty list unless "Any domain" is ticked, and shows the API refusal', () => {
    render({ email_domains: [], source: 'accounts' }, true);
    button('Change')!.click();
    fixture.detectChanges();
    fixture.componentInstance.save();
    fixture.detectChanges();
    expect(api.setRollCallDomains).not.toHaveBeenCalled();
    expect(el().textContent).toContain('Enter at least one domain');

    api.setRollCallDomains.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 422, error: { detail: [{ msg: "Value error, not a domain: 'x'" }] } })));
    fixture.componentInstance.text.set('x');
    fixture.componentInstance.save();
    fixture.detectChanges();
    expect(el().textContent).toContain('not a domain');
  });
});
