import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { AppNotification, NotificationsApiService } from '@core/services/notifications-api.service';
import { NotificationBellComponent } from './notification-bell.component';

const note = (id: string, read = false): AppNotification => ({
  id, title: `TN-${id}: new reply`, message: 'Rebooting now', level: 'info', read,
  link: `/support/${id}`, kind: 'ticket_reply', created_at: '2026-10-06T12:00:00Z',
});

describe('NotificationBellComponent', () => {
  let api: jasmine.SpyObj<NotificationsApiService>;

  beforeEach(() => {
    api = jasmine.createSpyObj('NotificationsApiService', ['unreadCount', 'list', 'markRead', 'markAllRead', 'settings', 'saveSettings']);
    api.unreadCount.and.returnValue(of({ unread: 2 }));
    api.list.and.returnValue(of([note('1'), note('2', true)]));
    api.markRead.and.returnValue(of(note('1', true)));
    api.markAllRead.and.returnValue(of({ unread: 0 }));
    api.settings.and.returnValue(of({ email_tickets: true, email_available: true }));
    api.saveSettings.and.callFake(on => of({ email_tickets: on, email_available: true }));
    TestBed.configureTestingModule({
      imports: [NotificationBellComponent],
      providers: [provideRouter([]), { provide: NotificationsApiService, useValue: api }],
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

  describe('email switch (#378)', () => {
    const open = () => {
      const fixture = TestBed.createComponent(NotificationBellComponent);
      fixture.detectChanges();
      fixture.componentInstance.toggle();
      fixture.detectChanges();
      return fixture;
    };
    const box = (f: ReturnType<typeof open>) =>
      (f.nativeElement as HTMLElement).querySelector('.foot input') as HTMLInputElement | null;

    it('shows the checkbox, on by default, once the panel is open', () => {
      const f = open();
      expect(box(f)?.checked).toBeTrue();
    });

    it('turning it off saves the choice', () => {
      const f = open();
      box(f)!.click();
      f.detectChanges();
      expect(api.saveSettings).toHaveBeenCalledOnceWith(false);
      expect(box(f)?.checked).toBeFalse();
    });

    it('puts the box back and says so when saving fails', () => {
      api.saveSettings.and.returnValue(throwError(() => new Error('down')));
      const f = open();
      box(f)!.click();
      f.detectChanges();
      expect(f.componentInstance.settings()?.email_tickets).toBeTrue();
      expect((f.nativeElement as HTMLElement).querySelector('.err')?.textContent).toContain('Could not save');
    });

    it('hides the switch when the install has no outgoing mail', () => {
      api.settings.and.returnValue(of({ email_tickets: true, email_available: false }));
      expect(box(open())).toBeNull();
    });

    it('hides it if the settings cannot be read', () => {
      api.settings.and.returnValue(throwError(() => new Error('down')));
      expect(box(open())).toBeNull();
    });
  });
});
