import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import type { components } from '../api/schema';

type S = components['schemas'];

export type TicketType = 'incident' | 'bug' | 'task' | 'request';
export type TicketStatus = 'open' | 'in_progress' | 'waiting' | 'resolved' | 'closed';
export type TicketPriority = 'low' | 'medium' | 'high' | 'critical';

export const STATUS_LABELS: Record<TicketStatus, string> = {
  open: 'Open',
  in_progress: 'In progress',
  waiting: 'Waiting on reporter',
  resolved: 'Resolved',
  closed: 'Closed',
};
export const STATUSES = Object.keys(STATUS_LABELS) as TicketStatus[];
export const PRIORITIES: TicketPriority[] = ['low', 'medium', 'high', 'critical'];
export const TYPE_LABELS: Record<TicketType, string> = {
  incident: "Something's broken",
  request: 'Request',
  task: 'Task',
  bug: 'Bug',
};

// Wire types alias the published contract (ADR 0002). The contract types status,
// priority and type as plain strings, so the UI narrows them to its unions; `Required<>`
// because these responses always carry every field.
type Narrowed<T> = Omit<Required<T>, 'status' | 'priority' | 'type'> & {
  status: TicketStatus;
  priority: TicketPriority;
  type: TicketType;
};
export type TicketSummary = Narrowed<S['TicketListOut']>;
export type Ticket = Narrowed<S['TicketOut']>;
export type TicketCreate = Omit<S['TicketIn'], 'type' | 'priority'> & { type?: TicketType; priority?: TicketPriority };
export type TicketUpdate = Omit<S['TicketUpdate'], 'status' | 'priority' | 'type'> & {
  status?: TicketStatus;
  priority?: TicketPriority;
  type?: TicketType;
};
export type TicketComment = Required<S['CommentOut']>;
export type TicketAttachment = S['AttachmentOut'];
export type TicketActivity = Required<S['ActivityOut']>;
export type SupportQueue = S['QueueOut'];
export type Assignee = S['AssigneeOut'];
export interface BoardColumn {
  status: TicketStatus;
  tickets: TicketSummary[];
}

export interface TicketFilters {
  scope?: 'mine' | 'assigned' | 'all';
  status?: string;
  priority?: TicketPriority;
  type?: TicketType;
  queue_id?: string;
  range_id?: string;
  q?: string;
}

/** Client for `/tickets` (control-plane/api/app/routers/tickets.py). */
@Injectable({ providedIn: 'root' })
export class TicketsApiService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.apiUrl}/tickets`;

  list(filters: TicketFilters = {}): Observable<TicketSummary[]> {
    let params = new HttpParams();
    for (const [k, v] of Object.entries(filters) as [string, string | undefined][]) {
      if (v) params = params.set(k, v);
    }
    return this.http.get<TicketSummary[]>(this.base, { params });
  }
  create(body: TicketCreate): Observable<Ticket> {
    return this.http.post<Ticket>(this.base, body);
  }
  get(id: string): Observable<Ticket> {
    return this.http.get<Ticket>(`${this.base}/${id}`);
  }
  update(id: string, body: TicketUpdate): Observable<Ticket> {
    return this.http.patch<Ticket>(`${this.base}/${id}`, body);
  }
  remove(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/${id}`);
  }
  board(filters: { queue_id?: string; assignee?: string } = {}): Observable<BoardColumn[]> {
    let params = new HttpParams();
    if (filters.queue_id) params = params.set('queue_id', filters.queue_id);
    if (filters.assignee) params = params.set('assignee', filters.assignee);
    return this.http.get<BoardColumn[]>(`${this.base}/board`, { params });
  }
  move(id: string, status: TicketStatus, boardOrder: number): Observable<TicketSummary> {
    return this.http.post<TicketSummary>(`${this.base}/${id}/move`, { status, board_order: boardOrder });
  }
  comments(id: string): Observable<TicketComment[]> {
    return this.http.get<TicketComment[]>(`${this.base}/${id}/comments`);
  }
  addComment(id: string, body: string, isInternal = false): Observable<TicketComment> {
    return this.http.post<TicketComment>(`${this.base}/${id}/comments`, { body, is_internal: isInternal });
  }
  attachments(id: string): Observable<TicketAttachment[]> {
    return this.http.get<TicketAttachment[]>(`${this.base}/${id}/attachments`);
  }
  upload(id: string, files: File[]): Observable<TicketAttachment[]> {
    const form = new FormData();
    files.forEach(f => form.append('files', f, f.name));
    return this.http.post<TicketAttachment[]>(`${this.base}/${id}/attachments`, form);
  }
  /** Fetched as a blob so the auth header goes with it; a plain link would not carry it. */
  download(attachmentId: string): Observable<Blob> {
    return this.http.get(`${this.base}/attachments/${attachmentId}`, { responseType: 'blob' });
  }
  removeAttachment(attachmentId: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/attachments/${attachmentId}`);
  }
  activity(id: string): Observable<TicketActivity[]> {
    return this.http.get<TicketActivity[]>(`${this.base}/${id}/activity`);
  }
  queues(): Observable<SupportQueue[]> {
    return this.http.get<SupportQueue[]>(`${this.base}/queues`);
  }
  createQueue(body: S['QueueIn']): Observable<SupportQueue> {
    return this.http.post<SupportQueue>(`${this.base}/queues`, body);
  }
  updateQueue(id: string, body: S['QueueUpdate']): Observable<SupportQueue> {
    return this.http.put<SupportQueue>(`${this.base}/queues/${id}`, body);
  }
  deleteQueue(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/queues/${id}`);
  }
  assignees(): Observable<Assignee[]> {
    return this.http.get<Assignee[]>(`${this.base}/assignees`);
  }
}
