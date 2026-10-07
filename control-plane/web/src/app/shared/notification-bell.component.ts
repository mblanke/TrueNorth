import { DatePipe } from '@angular/common';
import {
  ChangeDetectionStrategy, Component, DestroyRef, ElementRef, HostListener, OnInit, inject, signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router } from '@angular/router';
import { MatIconModule } from '@angular/material/icon';
import { catchError, filter, interval, of } from 'rxjs';
import { AppNotification, NotificationsApiService } from '@core/services/notifications-api.service';

/**
 * The toolbar bell: unread count, and a short list of the caller's notifications.
 * Polls the count every minute while the tab is visible and after each navigation;
 * opening the list fetches it fresh. Clicking an item marks it read and opens its link.
 */
@Component({
  selector: 'tn-notification-bell',
  standalone: true,
  imports: [DatePipe, MatIconModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button type="button" class="bell" (click)="toggle()" [attr.aria-expanded]="open()"
            [attr.aria-label]="unread() ? unread() + ' unread notifications' : 'Notifications'">
      <mat-icon>notifications</mat-icon>
      @if (unread()) { <span class="badge" aria-hidden="true">{{ unread() > 99 ? '99+' : unread() }}</span> }
    </button>
    @if (open()) {
      <div class="panel" role="dialog" aria-label="Notifications">
        <div class="head">
          <strong>Notifications</strong>
          @if (unread()) { <button type="button" class="link" (click)="readAll()">Mark all read</button> }
        </div>
        @for (n of items(); track n.id) {
          <button type="button" class="item" [class.unread]="!n.read" (click)="go(n)">
            <span class="title">{{ n.title }}</span>
            @if (n.message) { <span class="msg">{{ n.message }}</span> }
            <span class="when">{{ n.created_at | date: 'short' }}</span>
          </button>
        } @empty {
          <p class="empty">{{ loading() ? 'Loading…' : 'Nothing new.' }}</p>
        }
      </div>
    }
  `,
  styles: [`
    :host { position: relative; display: inline-block; margin-right: 8px; }
    .bell { position: relative; background: none; border: 0; color: var(--text-primary); cursor: pointer; padding: 6px; border-radius: 6px; line-height: 0; }
    .bell:hover { background: var(--accent-muted, rgba(0,0,0,.05)); }
    .badge { position: absolute; top: 0; right: 0; min-width: 16px; height: 16px; padding: 0 4px; border-radius: 8px; background: var(--alert, #c8102e); color: #fff; font-size: 10px; line-height: 16px; text-align: center; font-weight: 600; }
    .panel { position: absolute; right: 0; top: calc(100% + 6px); width: 340px; max-height: 420px; overflow-y: auto; z-index: 1000; background: var(--bg-card); color: var(--text-primary); border: 1px solid var(--border); border-radius: 8px; box-shadow: 0 8px 24px rgba(0,0,0,.14); }
    .head { display: flex; justify-content: space-between; align-items: center; padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: .88rem; }
    .link { background: none; border: 0; color: var(--accent); cursor: pointer; font: inherit; font-size: .8rem; padding: 0; }
    .item { display: block; width: 100%; text-align: left; background: none; border: 0; border-bottom: 1px solid var(--border); padding: 10px 12px; cursor: pointer; color: inherit; font: inherit; }
    .item:hover { background: var(--accent-muted, rgba(0,0,0,.04)); }
    .item.unread { border-left: 3px solid var(--accent); padding-left: 9px; }
    .item.unread .title { font-weight: 600; }
    .title, .msg, .when { display: block; font-size: .84rem; }
    .msg { color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .when { color: var(--text-muted); font-size: .74rem; margin-top: 2px; }
    .empty { padding: 16px 12px; margin: 0; color: var(--text-secondary); font-size: .86rem; }
    @media (max-width: 480px) { .panel { position: fixed; left: 8px; right: 8px; width: auto; top: 56px; } }
  `],
})
export class NotificationBellComponent implements OnInit {
  private readonly api = inject(NotificationsApiService);
  private readonly router = inject(Router);
  private readonly host = inject(ElementRef<HTMLElement>);
  private readonly destroyRef = inject(DestroyRef);

  readonly open = signal(false);
  readonly unread = signal(0);
  readonly items = signal<AppNotification[]>([]);
  readonly loading = signal(false);

  ngOnInit(): void {
    this.refreshCount();
    interval(60_000)
      .pipe(
        filter(() => typeof document === 'undefined' || document.visibilityState === 'visible'),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe(() => this.refreshCount());
    this.router.events
      .pipe(filter(e => e instanceof NavigationEnd), takeUntilDestroyed(this.destroyRef))
      .subscribe(() => this.refreshCount());
  }

  toggle(): void {
    this.open.set(!this.open());
    if (this.open()) this.load();
  }

  go(n: AppNotification): void {
    this.open.set(false);
    if (!n.read) {
      this.unread.set(Math.max(0, this.unread() - 1));
      this.items.set(this.items().map(i => (i.id === n.id ? { ...i, read: true } : i)));
      this.api.markRead(n.id).pipe(catchError(() => of(null))).subscribe();
    }
    if (n.link) this.router.navigateByUrl(n.link);
  }

  readAll(): void {
    this.api.markAllRead().pipe(catchError(() => of(null))).subscribe(() => {
      this.unread.set(0);
      this.items.set(this.items().map(i => ({ ...i, read: true })));
    });
  }

  @HostListener('document:click', ['$event'])
  onDocumentClick(ev: MouseEvent): void {
    if (this.open() && !this.host.nativeElement.contains(ev.target as Node)) this.open.set(false);
  }

  @HostListener('document:keydown.escape')
  onEscape(): void {
    this.open.set(false);
  }

  private refreshCount(): void {
    // Errors (signed out, API restarting) just leave the last count; the next poll retries.
    this.api.unreadCount().pipe(catchError(() => of(null))).subscribe(r => r && this.unread.set(r.unread));
  }

  private load(): void {
    this.loading.set(true);
    this.api.list().pipe(catchError(() => of([] as AppNotification[]))).subscribe(list => {
      this.items.set(list);
      this.loading.set(false);
      this.refreshCount();
    });
  }
}
