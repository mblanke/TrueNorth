import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { NotificationService } from '@core/services/notification.service';
import { MarkdownViewComponent } from '@shared/markdown/markdown-view.component';
import { WikiRevision, WikiApiService } from '@core/services/wiki-api.service';

/** Revision list for a page; view any revision, restore it as a new one. */
@Component({
  selector: 'tn-wiki-history',
  standalone: true,
  imports: [DatePipe, RouterLink, MatButtonModule, MarkdownViewComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="tn-kb-head">
      <div>
        <h1>History</h1>
        <p class="tn-kb-small tn-kb-muted">{{ revisions()[0]?.title }}</p>
      </div>
      <a mat-stroked-button [routerLink]="['/wiki', slug, pageId]">Back to page</a>
    </div>
    <section class="tn-kb-panel">
      <table class="tn-kb-table">
        <thead><tr><th style="width:56px">Rev</th><th>Change</th><th style="width:150px">By</th><th style="width:170px">When</th><th style="width:130px"></th></tr></thead>
        <tbody>
          @for (r of revisions(); track r.id; let first = $first) {
            <tr>
              <td>{{ r.revision_number }}</td>
              <td>{{ r.edit_summary || '—' }}</td>
              <td>{{ r.editor_name || '—' }}</td>
              <td class="tn-kb-small">{{ r.created_at | date: 'medium' }}</td>
              <td>
                <button type="button" class="tn-kb-linkbtn tn-kb-small" (click)="view(r)">View</button>
                @if (first) {
                  <span class="tn-kb-small tn-kb-muted"> · current</span>
                } @else {
                  · <button type="button" class="tn-kb-linkbtn tn-kb-small" (click)="restore(r)">Restore</button>
                }
              </td>
            </tr>
          }
        </tbody>
      </table>
    </section>
    @if (viewing(); as v) {
      <section class="tn-kb-panel">
        <h2>Revision {{ v.revision_number }}: {{ v.title }}</h2>
        <tn-markdown-view [source]="v.body" />
      </section>
    }
  `,
})
export class WikiHistoryComponent implements OnInit {
  private readonly wiki = inject(WikiApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly notify = inject(NotificationService);

  slug = '';
  pageId = '';
  readonly revisions = signal<WikiRevision[]>([]);
  readonly viewing = signal<WikiRevision | null>(null);

  ngOnInit(): void {
    this.slug = this.route.parent?.snapshot.paramMap.get('space') ?? '';
    this.pageId = this.route.snapshot.paramMap.get('pageId') ?? '';
    this.load();
  }

  private load(): void {
    this.wiki.revisions(this.pageId).subscribe({
      next: r => this.revisions.set(r),
      error: () => this.notify.error('Could not load the history'),
    });
  }

  view(r: WikiRevision): void {
    this.wiki.revision(this.pageId, r.revision_number).subscribe(full => this.viewing.set(full));
  }

  restore(r: WikiRevision): void {
    if (!confirm(`Restore revision ${r.revision_number}? It becomes the newest revision; nothing is lost.`)) return;
    const current = this.revisions()[0]?.revision_number ?? 0;
    this.wiki.restore(this.pageId, r.revision_number, current).subscribe({
      next: () => {
        this.notify.success(`Revision ${r.revision_number} restored`);
        this.wiki.treeChanged.next();
        this.router.navigate(['/wiki', this.slug, this.pageId]);
      },
      error: err => {
        if (err?.status === 409) {
          this.notify.error('Someone saved this page a moment ago. The history has been refreshed; check it and try again.');
          this.load();
        } else {
          this.notify.error('Could not restore that revision');
        }
      },
    });
  }
}
