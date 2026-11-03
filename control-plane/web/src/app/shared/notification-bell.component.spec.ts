import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { signal } from '@angular/core';
import { Subject, of } from 'rxjs';
import { AppNotification, NotificationsApiService } from '@core/services/notifications-api.service';
import { UserSocketService } from '@core/services/user-socket.service';
import { NotificationBellComponent } from './notification-bell.component';

const note = (id: string, read = false): AppNotification => ({
  id, title: `TN-${id}: new reply`, message: 'Rebooting now', level: 'info', read,
  link: `/support/${id}`, kind: 'ticket_reply', created_at: '2026-10-06T12:00:00Z',
});

describe('NotificationBellComponent', () => {
  let api: jasmine.SpyObj<NotificationsApiService>;
  let pushed: Subject<AppNotification>;
  let opened: Subject<void>;
  let connected: ReturnType<typeof signal<boolean>>;

  beforeEach(() => {
    api = jasmine.createSpyObj('NotificationsApiService', ['unreadCount', 'list', 'markRead', 'markAllRead']);
    api.unreadCount.and.returnValue(of({ unread: 2 }));
    api.list.and.returnValue(of([note('1'), note('2', true)]));
    api.markRead.and.returnValue(of(note('1', true)));
    api.markAllRead.and.returnValue(of({ unread: 0 }));
    pushed = new Subject<AppNotification>();
    opened = new Subject<void>();
    connected = signal(false);
    TestBed.configureTestingModule({
      imports: [NotificationBellComponent],
      providers: [
        provideRouter([]),
        { provide: NotificationsApiService, useValue: api },
        { provide: UserSocketService, useValue: { notifications$: pushed, opened$: opened, connected } },
      ],
    });
  });

  describe('live push (#377)', () => {
    it('adds a pushed notification and bumps the badge at once', () => {
      const fixture = TestBed.createComponent(NotificationBellComponent);
      fixture.detectChanges();
      fixture.componentInstance.toggle();
      fixture.detectChanges();
      pushed.next(note('9'));
      fixture.detectChanges();
      const el: HTMLElement = fixture.nativeElement;
      expect(el.querySelector('.badge')?.textContent?.trim()).toBe('3');
      expect(el.querySelectorAll('.item')[0].textContent).toContain('TN-9: new reply');
    });

    it('shows a push once even if the same notification is then listed', () => {
      const fixture = TestBed.createComponent(NotificationBellComponent);
      fixture.detectChanges();
      pushed.next(note('1'));
      pushed.next(note('1'));
      expect(fixture.componentInstance.items().filter(i => i.id === '1').length).toBe(1);
      expect(fixture.componentInstance.unread()).toBe(3);
    });

    it('catches up when the socket (re)opens', () => {
      const fixture = TestBed.createComponent(NotificationBellComponent);
      fixture.detectChanges();
      api.unreadCount.calls.reset();
      opened.next();
      expect(api.unreadCount).toHaveBeenCalledTimes(1);
    });
  });

  it('shows the unread count', () => {
    const fixture = TestBed.createComponent(NotificationBellComponent);
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.badge')?.textContent?.trim()).toBe('2');
  });

  it('opening lists notifications; clicking one marks it read and follows its link', () => {
    const fixture = TestBed.createComponent(NotificationBellComponent);
    const router = TestBed.inject(Router);
    spyOn(router, 'navigateByUrl').and.resolveTo(true);
    fixture.detectChanges();
    fixture.componentInstance.toggle();
    fixture.detectChanges();
    const items = (fixture.nativeElement as HTMLElement).querySelectorAll('.item');
    expect(items.length).toBe(2);
    (items[0] as HTMLButtonElement).click();
    expect(api.markRead).toHaveBeenCalledWith('1');
    expect(router.navigateByUrl).toHaveBeenCalledWith('/support/1');
    expect(fixture.componentInstance.open()).toBeFalse();
  });

  it('mark all read clears the badge', () => {
    const fixture = TestBed.createComponent(NotificationBellComponent);
    fixture.detectChanges();
    fixture.componentInstance.readAll();
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.badge')).toBeNull();
  });
});
