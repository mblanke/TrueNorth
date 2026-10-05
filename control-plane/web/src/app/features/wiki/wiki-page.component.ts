import { ChangeDetectionStrategy, Component, DestroyRef, OnInit, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { DatePipe } from '@angular/common';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { catchError, of, switchMap } from 'rxjs';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { MarkdownViewComponent } from '@shared/markdown/markdown-view.component';
import { isStaffRole } from '@shared/kb-roles';
import { WikiPage, WikiApiService } from '@core/services/wiki-api.service';

@Component({
  selector: 'tn-wiki-page',
  standalone: true,
  imports: [DatePipe, RouterLink, MatButtonModule, MarkdownViewComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (page(); as p) {
      @if (p.breadcrumbs.length) {
        <div class="tn-kb-crumbs">
          @for (c of p.breadcrumbs; track c.id; let last = $last) {
            <a [routerLink]="['/wiki', slug, c.id]">{{ c.title }}</a>{{ last ? '' : ' › ' }}
          }
        </div>
      }
      <div class="tn-kb-head">
        <div>
          <h1>{{ p.title }}</h1>
          <p class="tn-kb-small tn-kb-muted">
            Updated {{ p.updated_at | date: 'medium' }}{{ p.last_editor_name ? ' by ' + p.last_editor_name : '' }}
            · revision {{ p.revision_number }}
            @if (!p.is_published) { · <span class="tn-kb-tag">draft</span> }
          </p>
        </div>
        @if (canEdit()) {
          <div class="tn-kb-actions">
            <a mat-button [routerLink]="['/wiki', slug, p.id, 'history']">History</a>
            <a mat-stroked-button [routerLink]="['/wiki', slug, 'new']" [queryParams]="{ parent: p.id }">Add child page</a>
            <a mat-flat-button color="primary" [routerLink]="['/wiki', slug, p.id, 'edit']">Edit</a>
          </div>
        }
      </div>
      <article class="tn-kb-panel">
        @if (p.body.trim()) {
          <tn-markdown-view [source]="p.body" />
        } @else {
          <p class="tn-kb-muted">This page is empty.</p>
        }
      </article>
      @if (p.children.length) {
        <section class="tn-kb-panel">
          <h2>In this section</h2>
          @for (c of p.children; track c.id) {
            <div class="tn-kb-row"><a class="tn-kb-link" [routerLink]="['/wiki', slug, c.id]">{{ c.title }}</a></div>
          }
        </section>
      }
      @if (p.tags) {
        <p class="tn-kb-small tn-kb-muted" style="margin-top:12px">
          Tags: @for (t of tags(); track t) { <span class="tn-kb-tag" style="margin-right:4px">{{ t }}</span> }
        </p>
      }
      @if (canEdit()) {
        <p style="margin-top:18px">
          <button mat-button color="warn" type="button" (click)="remove(p)">Delete page</button>
        </p>
      }
    } @else if (missing()) {
      <div class="tn-kb-panel"><p>This page doesn't exist, or you don't have access to it.</p></div>
    } @else {
      <p class="tn-kb-muted">Loading…</p>
    }
  `,
})
export class WikiPageComponent implements OnInit {
  private readonly wiki = inject(WikiApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly auth = inject(AuthService);
  private readonly notify = inject(NotificationService);
  private readonly destroyRef = inject(DestroyRef);

  slug = '';
  readonly page = signal<WikiPage | null>(null);
  readonly missing = signal(false);
  readonly canEdit = computed(() => isStaffRole(this.auth.user()?.role));
  readonly tags = computed(() => (this.page()?.tags ?? '').split(',').map(t => t.trim()).filter(Boolean));

  ngOnInit(): void {
    this.slug = this.route.parent?.snapshot.paramMap.get('space') ?? '';
    this.route.paramMap
      .pipe(
        switchMap(params => {
          this.page.set(null);
          this.missing.set(false);
          return this.wiki.getPage(params.get('pageId') ?? '').pipe(catchError(() => of(null)));
        }),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe(p => (p ? this.page.set(p) : this.missing.set(true)));
  }

  remove(p: WikiPage): void {
    const extra = p.children.length ? ' Pages under it are deleted too.' : '';
    if (!confirm(`Delete “${p.title}”?${extra}`)) return;
    this.wiki.deletePage(p.id).subscribe({
      next: () => {
        this.notify.success('Page deleted');
        this.wiki.treeChanged.next();
        this.router.navigate(['/wiki', this.slug]);
      },
      error: () => this.notify.error('Could not delete the page'),
    });
  }
}
