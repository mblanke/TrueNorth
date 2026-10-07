import { ChangeDetectionStrategy, Component, DestroyRef, OnInit, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { NgTemplateOutlet } from '@angular/common';
import { ActivatedRoute, Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { KbStylesComponent } from '@shared/kb-styles.component';
import { isAdminRole, isStaffRole } from '@shared/kb-roles';
import { WikiApiService, WikiSpace, WikiTreeNode } from '@core/services/wiki-api.service';

/** A space: its page tree on the left, the routed page on the right. */
@Component({
  selector: 'tn-wiki-space',
  standalone: true,
  imports: [NgTemplateOutlet, RouterLink, RouterLinkActive, RouterOutlet, KbStylesComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <tn-kb-styles />
    <div class="tn-kb">
      <div class="tn-kb-crumbs"><a routerLink="/wiki">Wiki</a> › {{ space()?.name ?? '…' }}</div>
      <div class="tn-kb-split" style="margin-top:10px">
        <nav class="tn-kb-panel tn-kb-tree" aria-label="Pages in this space">
          <strong style="display:block;margin-bottom:8px">{{ space()?.name }}</strong>
          <ng-template #nodes let-list>
            @for (n of list; track n.id) {
              <a [routerLink]="['/wiki', slug, n.id]" routerLinkActive="active"
                 [class.draft]="!n.is_published">{{ n.title }}{{ n.is_published ? '' : ' (draft)' }}</a>
              @if (n.children?.length) {
                <div class="kids"><ng-container *ngTemplateOutlet="nodes; context: { $implicit: n.children }" /></div>
              }
            }
          </ng-template>
          <ng-container *ngTemplateOutlet="nodes; context: { $implicit: tree() }" />
          @if (!loading() && tree().length === 0) {
            <p class="tn-kb-small tn-kb-muted">No pages yet.</p>
          }
          @if (canEdit() && !space()?.is_archived) {
            <p style="margin:12px 0 0"><a class="tn-kb-link tn-kb-small" [routerLink]="['/wiki', slug, 'new']">+ New page</a></p>
          }
          @if (isAdmin() && space() && !space()!.is_archived) {
            <p style="margin:6px 0 0"><button type="button" class="tn-kb-linkbtn tn-kb-small" (click)="archive()">Archive this space</button></p>
          }
        </nav>
        <div style="min-width:0">
          @if (space()?.is_archived) {
            <div class="tn-kb-banner" role="status">
              <span>This space is archived: it is hidden from the wiki list and from students, and its pages are read-only.</span>
              @if (isAdmin()) { <button type="button" class="tn-kb-linkbtn" (click)="unarchive()">Unarchive</button> }
            </div>
          }
          @if (missing()) {
            <div class="tn-kb-panel"><p>This space doesn't exist, or you don't have access to it.</p></div>
          } @else {
            <router-outlet />
          }
        </div>
      </div>
    </div>
  `,
})
export class WikiSpaceComponent implements OnInit {
  private readonly wiki = inject(WikiApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly auth = inject(AuthService);
  private readonly notify = inject(NotificationService);
  private readonly destroyRef = inject(DestroyRef);

  slug = '';
  readonly space = signal<WikiSpace | null>(null);
  readonly tree = signal<WikiTreeNode[]>([]);
  readonly loading = signal(true);
  readonly missing = signal(false);
  readonly canEdit = computed(() => isStaffRole(this.auth.user()?.role));
  readonly isAdmin = computed(() => isAdminRole(this.auth.user()?.role));

  ngOnInit(): void {
    this.route.paramMap.pipe(takeUntilDestroyed(this.destroyRef)).subscribe(p => {
      this.slug = p.get('space') ?? '';
      this.load();
    });
    this.wiki.treeChanged.pipe(takeUntilDestroyed(this.destroyRef)).subscribe(() => this.loadTree());
  }

  archive(): void {
    if (!confirm(`Archive “${this.space()?.name}”? It disappears from the wiki list and from students; its pages are kept, read-only, and an administrator can unarchive it from the wiki list.`)) return;
    this.wiki.archiveSpace(this.slug).subscribe({
      next: () => { this.notify.success('Space archived'); this.router.navigate(['/wiki']); },
      error: () => this.notify.error('Could not archive the space'),
    });
  }

  unarchive(): void {
    this.wiki.updateSpace(this.slug, { is_archived: false }).subscribe({
      next: s => { this.space.set(s); this.wiki.spaceChanged.next(); this.notify.success('Space restored'); },
      error: () => this.notify.error('Could not unarchive the space'),
    });
  }

  private load(): void {
    this.missing.set(false);
    this.wiki.getSpace(this.slug).subscribe({
      next: s => { this.space.set(s); this.loadTree(); },
      error: () => { this.missing.set(true); this.loading.set(false); },
    });
  }

  private loadTree(): void {
    this.loading.set(true);
    this.wiki.tree(this.slug).subscribe({
      next: t => { this.tree.set(t); this.loading.set(false); },
      error: () => this.loading.set(false),
    });
  }
}
