import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { signal } from '@angular/core';
import { BehaviorSubject, of, throwError } from 'rxjs';
import { AuthService, CurrentUser } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { Ticket, TicketsApiService } from '@core/services/tickets-api.service';
import { apiErrorMessage } from '@shared/kb-errors';
import { TicketDetailComponent } from './ticket-detail.component';

function ticket(id: string, extra: Partial<Ticket> = {}): Ticket {
  return {
    id, number: 1, key: `TN-${id}`, type: 'incident', subject: `Ticket ${id}`, status: 'open', priority: 'medium',
    category: 'other', labels: '', queue_id: 'q1', queue_name: 'Support', reporter_id: 'student-1', reporter_name: 'S',
    assignee_id: null, assignee_name: '', range_id: null, exercise_id: null, board_order: 1,
    created_at: '2026-10-06T00:00:00Z', updated_at: '2026-10-06T00:00:00Z', description: '', range_name: '',
    exercise_name: '', resolved_at: null, closed_at: null, can_work: true, ...extra,
  } as Ticket;
}

describe('TicketDetailComponent', () => {
  let api: jasmine.SpyObj<TicketsApiService>;
  let params: BehaviorSubject<ReturnType<typeof convertToParamMap>>;
  const user = signal<CurrentUser | null>({ sub: 's', id: 'staff-1', email: 'e', display_name: 'n', role: 'instructor' });

  beforeEach(() => {
    params = new BehaviorSubject(convertToParamMap({ id: 'A' }));
    api = jasmine.createSpyObj('TicketsApiService', [
      'get', 'update', 'comments', 'attachments', 'activity', 'assignees', 'queues', 'categories', 'labels',
    ]);
    api.categories.and.returnValue(of([{ key: 'range', label: 'Range or lab' }, { key: 'other', label: 'Something else' }]));
    api.labels.and.returnValue(of([{ label: 'boot', count: 3 }]));
    api.get.and.callFake((id: string) => of(ticket(id)));
    api.comments.and.returnValue(of([]));
    api.attachments.and.returnValue(of([]));
    api.activity.and.returnValue(of([]));
    api.assignees.and.returnValue(of([]));
    api.queues.and.returnValue(of([]));
    TestBed.configureTestingModule({
      imports: [TicketDetailComponent],
      providers: [
        provideRouter([]),
        { provide: ActivatedRoute, useValue: { paramMap: params.asObservable() } },
        { provide: TicketsApiService, useValue: api },
        { provide: AuthService, useValue: { user } },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['error', 'success']) },
      ],
    });
  });

  it('follows a link to another ticket instead of keeping the first one', () => {
    const fixture = TestBed.createComponent(TicketDetailComponent);
    fixture.detectChanges();
    expect(fixture.componentInstance.ticket()?.id).toBe('A');

    params.next(convertToParamMap({ id: 'B' }));
    fixture.detectChanges();
    expect(api.get).toHaveBeenCalledWith('B');
    expect(fixture.componentInstance.ticket()?.id).toBe('B');
    expect((fixture.nativeElement as HTMLElement).querySelector('h1')?.textContent).toContain('Ticket B');
  });

  it('puts a select back when the server refuses the change', () => {
    const fixture = TestBed.createComponent(TicketDetailComponent);
    fixture.detectChanges();
    api.update.and.returnValue(throwError(() => ({ status: 422, error: { detail: 'Assignee must be staff' } })));
    const select = document.createElement('select');
    for (const v of ['open', 'closed']) {
      const o = document.createElement('option');
      o.value = v;
      select.appendChild(o);
    }
    select.value = 'closed';
    fixture.componentInstance.patchField('status', select);
    expect(select.value).toBe('open');
  });

  it('offers "remove" only to staff or to whoever uploaded the file', () => {
    user.set({ sub: 's', id: 'student-1', email: 'e', display_name: 'n', role: 'student' });
    api.get.and.callFake((id: string) => of(ticket(id, { can_work: false })));
    const fixture = TestBed.createComponent(TicketDetailComponent);
    fixture.detectChanges();
    const c = fixture.componentInstance;
    const file = { id: 'f', filename: 'a', content_type: 'x', size_bytes: 1, created_at: '' };
    expect(c.canRemove({ ...file, uploaded_by: 'student-1' })).toBeTrue();
    expect(c.canRemove({ ...file, uploaded_by: 'staff-1' })).toBeFalse();
    user.set({ sub: 's', id: 'staff-1', email: 'e', display_name: 'n', role: 'instructor' });
  });
});

describe('TicketDetailComponent labels and category (#376)', () => {
  let api: jasmine.SpyObj<TicketsApiService>;
  const user = signal<CurrentUser | null>({ sub: 's', id: 'staff-1', email: 'e', display_name: 'n', role: 'range_ops' });

  function create(over: Partial<Ticket> = {}, role: CurrentUser['role'] = 'range_ops') {
    user.set({ sub: 's', id: 'staff-1', email: 'e', display_name: 'n', role });
    api = jasmine.createSpyObj('TicketsApiService', [
      'get', 'update', 'comments', 'attachments', 'activity', 'assignees', 'queues', 'categories', 'labels',
    ]);
    api.get.and.callFake((id: string) => of(ticket(id, over)));
    api.update.and.callFake((_id: string, body: any) => of(ticket('A', { ...over, ...body })));
    for (const m of ['comments', 'attachments', 'activity', 'assignees', 'queues'] as const) {
      (api[m] as jasmine.Spy).and.returnValue(of([]));
    }
    api.categories.and.returnValue(of([{ key: 'range', label: 'Range or lab' }, { key: 'other', label: 'Something else' }]));
    api.labels.and.returnValue(of([{ label: 'boot', count: 3 }]));
    TestBed.configureTestingModule({
      imports: [TicketDetailComponent],
      providers: [
        provideRouter([]),
        { provide: ActivatedRoute, useValue: { paramMap: of(convertToParamMap({ id: 'A' })) } },
        { provide: TicketsApiService, useValue: api },
        { provide: AuthService, useValue: { user } },
        { provide: NotificationService, useValue: jasmine.createSpyObj('NotificationService', ['error', 'success']) },
      ],
    });
    const fixture = TestBed.createComponent(TicketDetailComponent);
    fixture.detectChanges();
    return fixture;
  }

  afterEach(() => TestBed.resetTestingModule());

  it('lists the ticket labels as chips for staff and adds one through the API', () => {
    const fixture = create({ labels: 'boot' });
    const el: HTMLElement = fixture.nativeElement;
    expect(Array.from(el.querySelectorAll('.tn-kb-chip')).map(c => c.textContent?.replace('×', '').trim())).toEqual(['boot']);

    const input = el.querySelector('input[aria-label="Add a label"]') as HTMLInputElement;
    input.value = 'esxi-03';
    fixture.componentInstance.addLabel(fixture.componentInstance.ticket()!, input);
    expect(api.update).toHaveBeenCalledWith('A', { labels: 'boot,esxi-03' });
    expect(input.value).toBe('');
  });

  it('removes a label', () => {
    const fixture = create({ labels: 'boot,esxi-03' });
    fixture.componentInstance.removeLabel(fixture.componentInstance.ticket()!, 'boot');
    expect(api.update).toHaveBeenCalledWith('A', { labels: 'esxi-03' });
  });

  it('changes the category through the select, and offers a legacy value the ticket already has', () => {
    const fixture = create({ category: 'legacy-thing' });
    const options = fixture.componentInstance.categoryOptions('legacy-thing').map(c => c.key);
    expect(options).toEqual(['range', 'other', 'legacy-thing']);
    const select = document.createElement('select');
    for (const v of ['other', 'range']) {
      const o = document.createElement('option');
      o.value = v;
      select.appendChild(o);
    }
    select.value = 'range';
    fixture.componentInstance.patchField('category', select);
    expect(api.update).toHaveBeenCalledWith('A', { category: 'range' });
  });

  it('shows a Student the category as text and offers no label controls', () => {
    const fixture = create({ can_work: false, category: 'range' }, 'student');
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('Range or lab');
    expect(el.querySelector('select[aria-label="Category"]')).toBeNull();
    expect(el.querySelector('input[aria-label="Add a label"]')).toBeNull();
    expect(api.labels).not.toHaveBeenCalled();
  });

  it('writes label and category changes in the history in plain words', () => {
    const fixture = create();
    const c = fixture.componentInstance;
    const row = (field: string, o: string, n: string) =>
      ({ id: 'x', actor_id: 'a', actor_name: 'Sam', field, old_value: o, new_value: n, created_at: '' });
    expect(c.describe(row('labels', 'boot', 'boot,esxi-03'))).toBe('Sam changed labels from boot to boot, esxi-03');
    expect(c.describe(row('category', 'other', 'range'))).toBe('Sam changed category from Something else to Range or lab');
  });
});

describe('apiErrorMessage', () => {
  it('passes a string detail through', () => {
    expect(apiErrorMessage({ error: { detail: 'Nope' } }, 'x')).toBe('Nope');
  });

  it('turns a 422 list into readable text instead of [object Object]', () => {
    const err = { error: { detail: [{ loc: ['body', 'slug'], msg: 'String should match pattern' }] } };
    expect(apiErrorMessage(err, 'x')).toBe('slug: String should match pattern');
  });

  it('falls back when there is nothing usable', () => {
    expect(apiErrorMessage(null, 'Could not save')).toBe('Could not save');
  });
});
