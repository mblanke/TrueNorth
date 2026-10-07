import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { AuthService } from '@core/services/auth.service';
import { isStaffRole } from '@shared/kb-roles';
import { WikiApiService } from '@core/services/wiki-api.service';

/** `/wiki/:space` with no page chosen: open the first page, or invite the first one. */
@Component({
  selector: 'tn-wiki-space-landing',
  standalone: true,
  imports: [RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (empty()) {
      <div class="tn-kb-panel">
        <h2>This space is empty</h2>
        @if (canEdit()) {
          <p class="tn-kb-muted">Write the first page.</p>
          <a class="tn-kb-link" [routerLink]="['/wiki', slug, 'new']">+ New page</a>
        } @else {
          <p class="tn-kb-muted">Nothing has been published here yet.</p>
        }
      </div>
    }
  `,
})
export class WikiSpaceLandingComponent implements OnInit {
  private readonly wiki = inject(WikiApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly auth = inject(AuthService);

  slug = '';
  readonly empty = signal(false);
  readonly canEdit = computed(() => isStaffRole(this.auth.user()?.role));

  ngOnInit(): void {
    this.slug = this.route.parent?.snapshot.paramMap.get('space') ?? '';
    this.wiki.tree(this.slug).subscribe({
      next: tree => {
        if (tree.length) {
          this.router.navigate(['/wiki', this.slug, tree[0].id], { replaceUrl: true });
        } else {
          this.empty.set(true);
        }
      },
      error: () => this.empty.set(true),
    });
  }
}
