import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '@env/environment';
import { GoldenImage, GoldenImageBuilds } from '../models';

/**
 * The golden-image library and its builds (ADR 0013 §2). The template kit registers builds;
 * an admin promotes one, and every new range then clones exactly that build.
 */
@Injectable({ providedIn: 'root' })
export class GoldenImagesApiService {
  private readonly http = inject(HttpClient);
  private readonly base = environment.apiUrl;

  list(hypervisor = 'vsphere'): Observable<GoldenImage[]> {
    return this.http.get<GoldenImage[]>(`${this.base}/golden-images`, { params: { hypervisor } });
  }

  /** infra:read. The image's builds, newest first, the promoted one, and its audit trail. */
  builds(imageId: string): Observable<GoldenImageBuilds> {
    return this.http.get<GoldenImageBuilds>(`${this.base}/golden-images/${imageId}/builds`);
  }

  /** The platform administrator only (403 otherwise). Audited; ranges then clone only this build. */
  promote(imageId: string, buildId: string, reason = ''): Observable<GoldenImageBuilds> {
    return this.http.post<GoldenImageBuilds>(
      `${this.base}/golden-images/${imageId}/builds/${encodeURIComponent(buildId)}/promote`, { reason });
  }
}
