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

  function create(role: CurrentUser['role']) {
    user.set({ sub: 's', id: 'u1', email: 'e', display_name: 'n', role });
    api = jasmine.createSpyObj('TicketsApiService', ['list']);
    api.list.and.returnValue(of([]));
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

  it('gives range ops the triage views and the board', () => {
    const fixture = create('range_ops');
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('Assigned to me');
    expect(el.textContent).toContain('Board');
    expect(el.textContent).not.toContain('Queues'); // admin only
    expect(api.list.calls.mostRecent().args[0]?.scope).toBe('all');
  });
});
