import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { signal } from '@angular/core';
import { of } from 'rxjs';
import { AuthService, CurrentUser } from '@core/services/auth.service';
import { TicketListComponent } from './ticket-list.component';
import { TicketsApiService } from '@core/services/tickets-api.service';

describe('TicketListComponent', () => {
  let api: jasmine.SpyObj<TicketsApiService>;
  const user = signal<CurrentUser | null>(null);
  const stubLookups = () => {
    api.categories.and.returnValue(of([{ key: 'range', label: 'Range or lab' }, { key: 'other', label: 'Something else' }]));
    api.labels.and.returnValue(of([{ label: 'boot', count: 2 }]));
  };

  function create(role: CurrentUser['role']) {
    user.set({ sub: 's', id: 'u1', email: 'e', display_name: 'n', role });
    api = jasmine.createSpyObj('TicketsApiService', ['list', 'categories', 'labels']);
    api.list.and.returnValue(of([]));
    stubLookups();
    try { sessionStorage.removeItem('tn-support-tab'); } catch { /* ignore */ }
    TestBed.configureTestingModule({
      imports: [TicketListComponent],
      providers: [
        provideRouter([]),
        { provide: TicketsApiService, useValue: api },
        { provide: AuthService, useValue: { user } },
      ],
    });
    const fixture = TestBed.createComponent(TicketListComponent);
    fixture.detectChanges();
    return fixture;
  }

  it('shows a student only their own tickets, with no board or all-tickets view', () => {
    const fixture = create('student');
    const el: HTMLElement = fixture.nativeElement;
    const tabs = Array.from(el.querySelectorAll('.tn-kb-tabs button')).map(b => b.textContent?.trim());
    expect(tabs).toEqual(['Open', 'Resolved & closed']);
    expect(el.textContent).not.toContain('Board');
    expect(api.list.calls.mostRecent().args[0]?.scope).toBe('mine');
  });

  // Issue #201: a critical ticket nobody owns sat wherever its update time put it.
  const t = (key: string, priority: string, assignee: string | null) => ({
    id: key, key, subject: `S ${key}`, status: 'open', priority, type: 'incident', assignee_id: assignee,
    assignee_name: assignee ? 'Rae' : '', reporter_name: 'Sam', queue_name: '', updated_at: '2026-10-19T00:00:00Z',
  });
  const SERVER_ORDER = [t('TN-1', 'low', 'u9'), t('TN-2', 'critical', 'u9'), t('TN-3', 'high', null),
    t('TN-4', 'critical', null), t('TN-5', 'medium', null)];
  const keys = (fixture: { nativeElement: HTMLElement }) =>
    Array.from(fixture.nativeElement.querySelectorAll('td.tn-kb-key')).map(c => c.textContent?.trim());

  it('puts critical unassigned tickets first for staff triage', () => {
    user.set({ sub: 's', id: 'u1', email: 'e', display_name: 'n', role: 'range_ops' });
    api = jasmine.createSpyObj('TicketsApiService', ['list', 'categories', 'labels']);
    api.list.and.returnValue(of(SERVER_ORDER as any));
    stubLookups();
    try { sessionStorage.removeItem('tn-support-tab'); } catch { /* ignore */ }
    TestBed.configureTestingModule({
      imports: [TicketListComponent],
      providers: [provideRouter([]), { provide: TicketsApiService, useValue: api }, { provide: AuthService, useValue: { user } }],
    });
    const fixture = TestBed.createComponent(TicketListComponent);
    fixture.detectChanges();
    expect(keys(fixture)).toEqual(['TN-4', 'TN-2', 'TN-3', 'TN-5', 'TN-1']);
  });

  it("keeps a Student's own tickets in the order the API gives", () => {
    user.set({ sub: 's', id: 'u1', email: 'e', display_name: 'n', role: 'student' });
    api = jasmine.createSpyObj('TicketsApiService', ['list', 'categories', 'labels']);
    api.list.and.returnValue(of(SERVER_ORDER as any));
    stubLookups();
    try { sessionStorage.removeItem('tn-support-tab'); } catch { /* ignore */ }
    TestBed.configureTestingModule({
      imports: [TicketListComponent],
      providers: [provideRouter([]), { provide: TicketsApiService, useValue: api }, { provide: AuthService, useValue: { user } }],
    });
    const fixture = TestBed.createComponent(TicketListComponent);
    fixture.detectChanges();
    expect(keys(fixture)).toEqual(['TN-1', 'TN-2', 'TN-3', 'TN-4', 'TN-5']);
  });

  it('gives range ops the triage views and the board', () => {
    const fixture = create('range_ops');
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('Assigned to me');
    expect(el.textContent).toContain('Board');
    expect(el.textContent).not.toContain('Queues'); // admin only
    expect(api.list.calls.mostRecent().args[0]?.scope).toBe('all');
  });

  describe('category and label filters (#376)', () => {
    it('sends the category to the API and shows its name on each row', () => {
      const fixture = create('student');
      api.list.and.returnValue(of([{ ...t('TN-9', 'low', null), category: 'range', labels: '' }] as any));
      fixture.componentInstance.category = 'range';
      fixture.componentInstance.load();
      fixture.detectChanges();
      expect(api.list.calls.mostRecent().args[0]?.category).toBe('range');
      expect((fixture.nativeElement as HTMLElement).textContent).toContain('Range or lab');
    });

    it('offers the label filter, and label chips, to staff only', () => {
      const staff = create('range_ops');
      api.list.and.returnValue(of([{ ...t('TN-9', 'low', null), category: 'range', labels: 'boot,esxi-03' }] as any));
      staff.componentInstance.label = 'boot';
      staff.componentInstance.load();
      staff.detectChanges();
      expect(api.list.calls.mostRecent().args[0]?.label).toBe('boot');
      expect((staff.nativeElement as HTMLElement).querySelector('#tk-f-label')).not.toBeNull();
      expect(Array.from((staff.nativeElement as HTMLElement).querySelectorAll('.tn-kb-chip')).map(c => c.textContent?.trim()))
        .toEqual(['boot', 'esxi-03']);
    });

    it('never asks for labels or shows them for a Student, even if one were sent', () => {
      TestBed.resetTestingModule();
      const fixture = create('student');
      api.list.and.returnValue(of([{ ...t('TN-9', 'low', null), category: 'range', labels: 'user-error' }] as any));
      fixture.componentInstance.label = 'user-error';
      fixture.componentInstance.load();
      fixture.detectChanges();
      expect(api.list.calls.mostRecent().args[0]?.label).toBeUndefined();
      expect(api.labels).not.toHaveBeenCalled();
      expect((fixture.nativeElement as HTMLElement).textContent).not.toContain('user-error');
    });
  });
});
