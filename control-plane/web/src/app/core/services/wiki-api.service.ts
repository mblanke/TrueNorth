import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable, Subject } from 'rxjs';
import { environment } from '@env/environment';

import type { components } from '../api/schema';

type S = components['schemas'];

// Wire types alias the published contract (ADR 0002). `Required<>` because these
// responses always carry every field; the contract marks defaulted ones optional.
export type WikiVisibility = 'all' | 'staff';
export type WikiSpace = Required<S['SpaceOut']>;
export type WikiTreeNode = S['TreeNode'];
export type WikiPage = Required<S['PageOut']>;
export type WikiPageCreate = S['PageIn'];
export type WikiPageUpdate = S['PageUpdate'];
export type WikiRevision = Required<S['RevisionListOut']> & { body?: string };
export type WikiSearchHit = S['SearchHit'];

/** Client for `/wiki` (control-plane/api/app/routers/wiki.py). */
@Injectable({ providedIn: 'root' })
export class WikiApiService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.apiUrl}/wiki`;

  /** Fires when pages are added, renamed, moved or removed, so the tree reloads. */
  readonly treeChanged = new Subject<void>();

  listSpaces(includeArchived = false): Observable<WikiSpace[]> {
    const params = new HttpParams().set('include_archived', includeArchived);
    return this.http.get<WikiSpace[]>(`${this.base}/spaces`, { params });
  }
  getSpace(slug: string): Observable<WikiSpace> {
    return this.http.get<WikiSpace>(`${this.base}/spaces/${encodeURIComponent(slug)}`);
  }
  createSpace(body: S['SpaceIn']): Observable<WikiSpace> {
    return this.http.post<WikiSpace>(`${this.base}/spaces`, body);
  }
  updateSpace(slug: string, body: S['SpaceUpdate']): Observable<WikiSpace> {
    return this.http.put<WikiSpace>(`${this.base}/spaces/${encodeURIComponent(slug)}`, body);
  }
  archiveSpace(slug: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/spaces/${encodeURIComponent(slug)}`);
  }
  tree(slug: string): Observable<WikiTreeNode[]> {
    return this.http.get<WikiTreeNode[]>(`${this.base}/spaces/${encodeURIComponent(slug)}/tree`);
  }
  createPage(spaceSlug: string, body: WikiPageCreate): Observable<WikiPage> {
    return this.http.post<WikiPage>(`${this.base}/spaces/${encodeURIComponent(spaceSlug)}/pages`, body);
  }
  getPage(id: string): Observable<WikiPage> {
    return this.http.get<WikiPage>(`${this.base}/pages/${id}`);
  }
  /** A stale `base_revision` comes back as HTTP 409 with `error.current` holding the page. */
  updatePage(id: string, body: WikiPageUpdate): Observable<WikiPage> {
    return this.http.put<WikiPage>(`${this.base}/pages/${id}`, body);
  }
  deletePage(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/pages/${id}`);
  }
  revisions(id: string): Observable<WikiRevision[]> {
    return this.http.get<WikiRevision[]>(`${this.base}/pages/${id}/revisions`);
  }
  revision(id: string, n: number): Observable<WikiRevision> {
    return this.http.get<WikiRevision>(`${this.base}/pages/${id}/revisions/${n}`);
  }
  /** `baseRevision` is the page revision the restore was decided against; stale is a 409. */
  restore(id: string, n: number, baseRevision: number): Observable<WikiPage> {
    return this.http.post<WikiPage>(`${this.base}/pages/${id}/revisions/${n}/restore`, { base_revision: baseRevision });
  }
  search(q: string): Observable<WikiSearchHit[]> {
    return this.http.get<WikiSearchHit[]>(`${this.base}/search`, { params: new HttpParams().set('q', q) });
  }
}
