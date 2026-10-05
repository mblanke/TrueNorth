import { ChangeDetectionStrategy, ChangeDetectorRef, Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { HttpErrorResponse } from '@angular/common/http';
import { NotificationService } from '@core/services/notification.service';
import { MarkdownEditorComponent } from '@shared/markdown/markdown-editor.component';
import { WikiPage, WikiApiService, WikiTreeNode } from '@core/services/wiki-api.service';

interface ParentOption { id: string; label: string }

/**
 * Create (`/wiki/:space/new?parent=`) or edit (`/wiki/:space/:pageId/edit`) a page.
 *
 * Saves send the revision the edit started from. If someone else saved meanwhile
 * the API answers 409 with their version; the editor keeps the user's text, shows
 * theirs, and lets them choose rather than silently overwriting either.
 */
@Component({
  selector: 'tn-wiki-edit',
  standalone: true,
  imports: [FormsModule, RouterLink, MatButtonModule, MarkdownEditorComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <form (ngSubmit)="save()">
      <div class="tn-kb-head">
        <div>
          <h1>{{ pageId ? 'Edit page' : 'New page' }}</h1>
          @if (base()) { <p class="tn-kb-small tn-kb-muted">Started from revision {{ base() }}</p> }
        </div>
        <div class="tn-kb-actions">
          <a mat-button [routerLink]="pageId ? ['/wiki', slug, pageId] : ['/wiki', slug]">Cancel</a>
          <button mat-flat-button color="primary" type="submit" [disabled]="saving() || !title.trim()">
            {{ saving() ? 'Saving…' : 'Save' }}
          </button>
        </div>
      </div>

      @if (conflict(); as theirs) {
        <div class="tn-kb-error" role="alert">
          <strong>{{ theirs.last_editor_name || 'Someone' }} saved this page while you were editing (now revision {{ theirs.revision_number }}).</strong>
          <p class="tn-kb-small" style="margin:6px 0">Your text is still below. Copy anything you need, then either keep your version
            (it replaces theirs, and theirs stays in History) or load theirs and start again.</p>
          <div class="tn-kb-actions">
            <button mat-stroked-button type="button" (click)="keepMine(theirs)">Save my version anyway</button>
            <button mat-button type="button" (click)="loadTheirs(theirs)">Discard mine, load theirs</button>
          </div>
        </div>
      }

      <div class="tn-kb-panel">
        <label class="tn-kb-field" for="wp-title" style="margin-top:0">Title</label>
        <input id="wp-title" class="tn-kb-input" name="title" required maxlength="500" [(ngModel)]="title">

        <span class="tn-kb-field">Content (Markdown)</span>
        <tn-markdown-editor label="Page content" [(value)]="body" />

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
          <div>
            <label class="tn-kb-field" for="wp-parent">Sits under</label>
            <select id="wp-parent" class="tn-kb-input" name="parent" [(ngModel)]="parentId">
              <option [ngValue]="null">Top level of the space</option>
              @for (o of parents(); track o.id) { <option [ngValue]="o.id">{{ o.label }}</option> }
            </select>
          </div>
          <div>
            <label class="tn-kb-field" for="wp-tags">Tags (comma separated)</label>
            <input id="wp-tags" class="tn-kb-input" name="tags" maxlength="500" [(ngModel)]="tags">
          </div>
        </div>
        <label class="tn-kb-small" style="display:flex;gap:6px;align-items:center;margin-top:12px">
          <input type="checkbox" name="published" [(ngModel)]="published"> Published (students can read it)
        </label>
        @if (pageId) {
          <label class="tn-kb-field" for="wp-summary">What changed? (optional)</label>
          <input id="wp-summary" class="tn-kb-input" name="summary" maxlength="500" placeholder="e.g. added the paused-exercise step"
                 [(ngModel)]="summary">
        }
      </div>
    </form>
  `,
})
export class WikiEditComponent implements OnInit {
  private readonly wiki = inject(WikiApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly notify = inject(NotificationService);
  private readonly cdr = inject(ChangeDetectorRef);

  slug = '';
  pageId: string | null = null;
  title = '';
  body = '';
  tags = '';
  summary = '';
  published = true;
  parentId: string | null = null;
  private originalParent: string | null = null;

  readonly base = signal(0);
  readonly saving = signal(false);
  readonly conflict = signal<WikiPage | null>(null);
  readonly parents = signal<ParentOption[]>([]);

  ngOnInit(): void {
    this.slug = this.route.parent?.snapshot.paramMap.get('space') ?? '';
    this.pageId = this.route.snapshot.paramMap.get('pageId');
    this.parentId = this.route.snapshot.queryParamMap.get('parent');
    if (this.pageId) {
      this.wiki.getPage(this.pageId).subscribe({
        next: p => this.fill(p),
        error: () => this.notify.error('Could not load the page'),
      });
    }
    this.wiki.tree(this.slug).subscribe(tree => this.parents.set(this.flatten(tree)));
  }

  save(force = false): void {
    if (!this.title.trim()) return;
    this.saving.set(true);
    const req$ = this.pageId
      ? this.wiki.updatePage(this.pageId, {
          base_revision: this.base(),
          title: this.title.trim(),
          body: this.body,
          tags: this.tags,
          is_published: this.published,
          edit_summary: force ? `${this.summary} (kept over a concurrent edit)`.trim() : this.summary,
          ...(this.parentId !== this.originalParent
            ? this.parentId ? { parent_id: this.parentId } : { move_to_root: true }
            : {}),
        })
      : this.wiki.createPage(this.slug, {
          title: this.title.trim(),
          body: this.body,
          parent_id: this.parentId,
          tags: this.tags,
          is_published: this.published,
        });
    req$.subscribe({
      next: p => {
        this.saving.set(false);
        this.notify.success('Saved');
        this.wiki.treeChanged.next();
        this.router.navigate(['/wiki', this.slug, p.id]);
      },
      error: (err: HttpErrorResponse) => {
        this.saving.set(false);
        if (err.status === 409 && err.error?.current) {
          this.conflict.set(err.error.current as WikiPage);
        } else {
          this.notify.error(typeof err.error?.detail === 'string' ? err.error.detail : 'Could not save the page');
        }
      },
    });
  }

  keepMine(theirs: WikiPage): void {
    this.base.set(theirs.revision_number);
    this.originalParent = theirs.parent_id;
    this.conflict.set(null);
    this.save(true);
  }

  loadTheirs(theirs: WikiPage): void {
    this.fill(theirs);
    this.conflict.set(null);
  }

  private fill(p: WikiPage): void {
    this.title = p.title;
    this.body = p.body;
    this.tags = p.tags;
    this.published = p.is_published;
    this.parentId = p.parent_id;
    this.originalParent = p.parent_id;
    this.base.set(p.revision_number);
    this.cdr.markForCheck();
  }

  /** Every page except this one and its descendants, indented by depth. */
  private flatten(nodes: WikiTreeNode[], depth = 0): ParentOption[] {
    return nodes.flatMap(n =>
      n.id === this.pageId
        ? []
        : [{ id: n.id, label: `${'— '.repeat(depth)}${n.title}` }, ...this.flatten(n.children ?? [], depth + 1)],
    );
  }
}
