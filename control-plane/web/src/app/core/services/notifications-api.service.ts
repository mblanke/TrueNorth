import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

/** One in-app notification (GET /notifications). Aliased from the contract (ADR 0002). */
export type AppNotification = Required<S['NotificationOut']>;

/** Client for `/notifications` (control-plane/api/app/routers/notifications.py): the caller's own only. */
@Injectable({ providedIn: 'root' })
export class NotificationsApiService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.apiUrl}/notifications`;

  list(unreadOnly = false, limit = 30): Observable<AppNotification[]> {
    const params = new HttpParams().set('unread_only', unreadOnly).set('limit', limit);
    return this.http.get<AppNotification[]>(this.base, { params });
  }
  unreadCount(): Observable<S['NotificationUnreadCountOut']> {
    return this.http.get<S['NotificationUnreadCountOut']>(`${this.base}/unread-count`);
  }
  markRead(id: string): Observable<AppNotification> {
    return this.http.post<AppNotification>(`${this.base}/${id}/read`, {});
  }
  markAllRead(): Observable<S['NotificationUnreadCountOut']> {
    return this.http.post<S['NotificationUnreadCountOut']>(`${this.base}/read-all`, {});
  }
}
