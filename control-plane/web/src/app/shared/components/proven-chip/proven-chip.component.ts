import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { DatePipe } from '@angular/common';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { AuthService } from '@core/services/auth.service';
import { ContentPortApiService, PORTABLE_TYPES, ProvenStatus } from '@core/services/content-port-api.service';
import { apiErrorMessage } from '../../kb-errors';

const KIND_LABEL: Record<string, string> = {
  certification: 'Certification record', class: 'Class', exercise: 'Exercise', review: 'Content clock',
};
const TYPE_LABEL: Record<string, string> = { course: 'Course', range_template: 'Range template', scenario: 'Scenario',
  exercise_template: 'Exercise template' };

/**
 * The Proven chip (docs/content-port.md): "Proven", or "Not proven: <reasons>", for a
 * course, range template or scenario, with its evidence (the certification record, the
 * class or exercise, the content clock) behind "Why". Computed by the API every time.
 * Shown only to staff who read the content clock (content_review:read); nothing for other
 * content types.
 */
@Component({
  selector: 'tn-proven-chip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe, MatButtonModule, MatIconModule],
  template: `
    @if (shown()) {
      <div class="proven">
        @if (status(); as st) {
          <div class="row">
            <span class="chip" [class.ok]="st.proven" role="status">
              <mat-icon aria-hidden="true">{{ st.proven ? 'verified' : 'pending' }}</mat-icon>
              <span class="text">{{ st.label }}</span>
            </span>
            <button mat-button type="button" class="why" (click)="open.set(!open())" [attr.aria-expanded]="open()">
              {{ open() ? 'Hide' : 'Why' }}
            </button>
          </div>
          @if (st.imported; as imp) {
            <p class="imported">Ported here {{ imp.imported_at | date: 'd MMM y' }} from key {{ imp.origin_key_id }}
              ({{ imp.proven_at_origin ? 'proven at origin' : 'not proven at origin' }}). It must be proven on this
              site too.</p>
          }
          @if (open()) {
            <ul class="conditions" aria-label="Proven conditions">
              @for (c of st.conditions; track c.name) {
                <li [class.met]="c.met">
                  <mat-icon aria-hidden="true">{{ c.met ? 'check_circle' : 'cancel' }}</mat-icon>
                  <div class="body">
                    <div class="title">{{ c.title }}</div>
                    @for (r of c.reasons ?? []; track $index) { <div class="reason">{{ r }}</div> }
                    @for (e of c.evidence ?? []; track e.id) {
                      <div class="evidence">
                        <span class="kind">{{ kindLabel(e.kind) }}</span> {{ e.label }}
                        <code>{{ e.id }}</code>
                        @if (e.at) { · {{ e.at | date: 'd MMM y' }} }
                      </div>
                    }
                  </div>
                </li>
              }
            </ul>
            @if (st.dependencies?.length) {
              <div class="deps">
                <div class="title">Needs</div>
                @for (d of st.dependencies ?? []; track d.item_id) {
                  <div class="dep">
                    <mat-icon aria-hidden="true">{{ d.proven ? 'verified' : 'pending' }}</mat-icon>
                    {{ typeLabel(d.item_type) }} {{ d.title ?? d.item_id }}:
                    {{ d.proven === null ? 'missing here' : d.proven ? 'proven' : 'not proven' }}
                  </div>
                }
              </div>
            }
            @if (st.golden_builds?.length) {
              <div class="deps">
                <div class="title">Golden template builds</div>
                @for (b of st.golden_builds ?? []; track b.catalogue_id + b.build_id) {
                  <div class="dep"><code>{{ b.catalogue_id }} {{ b.build_id }}</code></div>
                }
              </div>
            }
          }
        } @else if (error()) {
          <p class="err" role="alert">{{ error() }}</p>
        } @else {
          <span class="chip loading">Checking proven…</span>
        }
      </div>
    }
  `,
  styles: [`
    .proven { display: flex; flex-direction: column; gap: 6px; }
    .row { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
    .chip { display: inline-flex; align-items: flex-start; gap: 6px; padding: 5px 10px; border-radius: 10px;
      font-size: 12px; font-weight: 600; background: #FFFAEB; color: #B54708; max-width: 100%; }
    .chip.ok { background: #ECFDF3; color: #067647; }
    .chip.loading { background: var(--mat-sys-surface-container-high); color: var(--mat-sys-on-surface-variant); }
    .chip mat-icon { font-size: 16px; width: 16px; height: 16px; flex: none; }
    .chip .text { overflow-wrap: anywhere; }
    .why { min-width: 0; font-size: 12px; }
    .imported { margin: 0; font-size: 12px; color: var(--mat-sys-on-surface-variant); }
    .conditions { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
    .conditions li { display: flex; gap: 6px; font-size: 12px; color: #B42318; }
    .conditions li.met { color: #067647; }
    .conditions mat-icon, .dep mat-icon { font-size: 16px; width: 16px; height: 16px; flex: none; }
    .body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
    .title { font-weight: 600; color: var(--mat-sys-on-surface); font-size: 12px; }
    .reason { color: #B42318; }
    .evidence { color: var(--mat-sys-on-surface-variant); overflow-wrap: anywhere; }
    .kind { font-weight: 600; }
    code { font-size: 11px; }
    .deps { display: flex; flex-direction: column; gap: 2px; font-size: 12px; }
    .dep { display: flex; align-items: center; gap: 4px; color: var(--mat-sys-on-surface-variant); }
    .err { margin: 0; font-size: 12px; color: var(--mat-sys-error); }
  `],
})
export class ProvenChipComponent {
  private readonly api = inject(ContentPortApiService);
  private readonly auth = inject(AuthService);

  readonly itemType = input.required<string>();
  readonly itemId = input.required<string>();

  readonly status = signal<ProvenStatus | null>(null);
  readonly error = signal('');
  readonly open = signal(false);
  readonly shown = computed(() => PORTABLE_TYPES.includes(this.itemType()) && this.auth.can('content_review:read'));

  constructor() {
    effect(() => {
      const type = this.itemType();
      const id = this.itemId();
      if (!this.shown()) return;
      untracked(() => this.load(type, id));
    });
  }

  kindLabel(kind: string): string {
    return KIND_LABEL[kind] ?? kind;
  }
  typeLabel(type: string): string {
    return TYPE_LABEL[type] ?? type;
  }

  private load(type: string, id: string): void {
    this.status.set(null);
    this.error.set('');
    this.open.set(false);
    this.api.proven(type, id).subscribe({
      next: st => this.status.set(st),
      error: e => this.error.set(apiErrorMessage(e, 'Could not read whether it is proven.')),
    });
  }
}
