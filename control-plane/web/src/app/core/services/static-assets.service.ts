import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

/**
 * Static JSON shipped with the web app under /assets (not the API, so no base URL).
 * Keeps features off HttpClient (ADR 0003) for the few files they read at runtime.
 */
@Injectable({ providedIn: 'root' })
export class StaticAssetsService {
  private readonly http = inject(HttpClient);

  json<T>(path: string): Observable<T> {
    return this.http.get<T>(path);
  }
}
