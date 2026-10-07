import { ChangeDetectionStrategy, Component, DestroyRef, OnInit, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { Subject, catchError, debounceTime, distinctUntilChanged, of, switchMap } from 'rxjs';
import { AuthService } from '@core/services/auth.service';
import { NotificationService } from '@core/services/notification.service';
import { KbStylesComponent } from '@shared/kb-styles.component';
import { apiErrorMessage } from '@shared/kb-errors';
import { isAdminRole } from '@shared/kb-roles';
import { WikiSearchHit, WikiApiService, WikiSpace, WikiVisibility } from '@core/services/wiki-api.service';

@Component({
  selector: 'tn-wiki-home',
  standalone: true,
  imports: [FormsModule, RouterLink, MatButtonModule, KbStylesComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <tn-kb-styles />
    <div class="tn-kb">
      <div class="tn-kb-head">
        <div>
          <h1>Wiki</h1>
          <p class="tn-kb-muted">How-tos, runbooks and course notes.</p>
        </div>
        <div class="tn-kb-actions">
          <input class="tn-kb-input" style="width:260px" type="search" placeholder="Search the wiki"
                 aria-label="Search the wiki" [ngModel]="query()" (ngModelChange)="onQuery($event)">
          @if (isAdmin()) {
            <label class="tn-kb-small" style="display:flex;gap:6px;align-items:center">
              <input type="checkbox" [ngModel]="showArchived()" (ngModelChange)="toggleArchived($event)"> Show archived
            </label>
            <button mat-stroked-button type="button" (click)="creating.set(!creating())">New space</button>
          }
        </div>
      </div>

      @if (creating()) {
        <form class="tn-kb-panel" style="max-width:640px;margin-bottom:14px" (ngSubmit)="create()">
          <h2>New space</h2>
          <label class="tn-kb-field" for="sp-name">Name</label>
          <input id="sp-name" class="tn-kb-input" name="name" required [(ngModel)]="draft.name" (ngModelChange)="autoSlug()">
          <label class="tn-kb-field" for="sp-slug">Address (lowercase letters, numbers and dashes)</label>
          <input id="sp-slug" class="tn-kb-input" name="slug" required pattern="[a-z0-9][a-z0-9-]*" [(ngModel)]="draft.slug" (input)="slugTouched = true">
          <label class="tn-kb-field" for="sp-desc">Description</label>
          <input id="sp-desc" class="tn-kb-input" name="description" [(ngModel)]="draft.description">
          <label class="tn-kb-field" for="sp-vis">Who can read it</label>
          <select id="sp-vis" class="tn-kb-input" name="visibility" [(ngModel)]="draft.visibility">
            <option value="all">Everyone, including students</option>
            <option value="staff">Staff only</option>
          </select>
          <div class="tn-kb-actions" style="margin-top:14px">
            <button mat-flat-button color="primary" type="submit" [disabled]="!draft.name || !draft.slug">Create space</button>
            <button mat-button type="button" (click)="creating.set(false)">Cancel</button>
          </div>
        </form>
      }

      @if (query().trim().length >= 2) {
        <section class="tn-kb-panel" aria-live="polite">
          <h2>Results for “{{ query().trim() }}”</h2>
          @for (h of hits(); track h.page_id) {
            <div class="tn-kb-row">
              <div>
                <a class="tn-kb-link" [routerLink]="['/wiki', h.space_slug, h.page_id]"><strong>{{ h.title }}</strong></a>
                <p class="tn-kb-small tn-kb-muted">{{ h.space_name }} · {{ h.snippet }}</p>
              </div>
            </div>
          } @empty {
            <p class="tn-kb-muted">{{ searching() ? 'Searching…' : 'No pages match.' }}</p>
          }
        </section>
      } @else {
        <section class="tn-kb-panel">
          @for (s of spaces(); track s.id) {
            <div class="tn-kb-row">
              <div>
                <a class="tn-kb-link" [routerLink]="['/wiki', s.slug]"><strong>{{ s.name }}</strong></a>
                @if (s.visibility === 'staff') { <span class="tn-kb-tag" style="margin-left:6px">staff only</span> }
                @if (s.is_archived) { <span class="tn-kb-tag" style="margin-left:6px">archived</span> }
                @if (s.description) { <p class="tn-kb-small tn-kb-muted">{{ s.description }}</p> }
              </div>
            </div>
          } @empty {
            <p class="tn-kb-muted">{{ loading() ? 'Loading…' : isAdmin() ? 'No spaces yet. Create one to start writing.' : 'Nothing has been published yet.' }}</p>
          }
        </section>
      }
    </div>
  `,
})
export class WikiHomeComponent implements OnInit {
  private readonly wiki = inject(WikiApiService);
  private readonly auth = inject(AuthService);
  private readonly notify = inject(NotificationService);
  private readonly destroyRef = inject(DestroyRef);

  readonly spaces = signal<WikiSpace[]>([]);
  readonly hits = signal<WikiSearchHit[]>([]);
  readonly query = signal('');
  readonly loading = signal(true);
  readonly searching = signal(false);
  readonly creating = signal(false);
  readonly showArchived = signal(false);
  readonly isAdmin = computed(() => isAdminRole(this.auth.user()?.role));

  draft = { name: '', slug: '', description: '', visibility: 'all' as WikiVisibility };
  slugTouched = false;
  private readonly query$ = new Subject<string>();

  ngOnInit(): void {
    this.load();
    this.query$
      .pipe(
        debounceTime(250),
        distinctUntilChanged(),
        switchMap(q => {
          this.searching.set(q.length >= 2);
          return q.length >= 2 ? this.wiki.search(q).pipe(catchError(() => of([]))) : of([]);
        }),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe(hits => { this.hits.set(hits); this.searching.set(false); });
  }

  onQuery(q: string): void {
    this.query.set(q);
    this.query$.next(q.trim());
  }

  autoSlug(): void {
    if (this.slugTouched) return;
    this.draft.slug = this.draft.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 100);
  }

  create(): void {
    this.wiki.createSpace(this.draft).subscribe({
      next: () => {
        this.notify.success('Space created');
        this.creating.set(false);
        this.draft = { name: '', slug: '', description: '', visibility: 'all' };
        this.slugTouched = false;
        this.load();
      },
      error: err => this.notify.error(apiErrorMessage(err, 'Could not create the space')),
    });
  }

  toggleArchived(on: boolean): void {
    this.showArchived.set(on);
    this.load();
  }

  private load(): void {
    this.loading.set(true);
    this.wiki.listSpaces(this.showArchived()).subscribe({
      next: s => { this.spaces.set(s); this.loading.set(false); },
      error: () => { this.loading.set(false); this.notify.error('Could not load the wiki'); },
    });
  }
}
