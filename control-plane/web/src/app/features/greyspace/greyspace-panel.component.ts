import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import {
  GreyspaceApiService,
  GreyspaceBlock,
  GreyspaceConfig,
  GreyspaceCorpus,
  GreyspaceStatus,
} from '@core/services/greyspace-api.service';
import { NotificationService } from '@core/services/notification.service';

type Tier = GreyspaceBlock['corpus_tier'];
type Npc = GreyspaceBlock['npc_profile'];

interface Draft {
  corpus_tier: Tier;
  packs: Record<string, boolean>;
  public_prefix: string;
  npc_profile: Npc;
  threat_infra: boolean;
  trust_ca: boolean;
}

const STATUS_LABEL: Record<string, string> = {
  not_attached: 'Not attached',
  configured: 'Configured',
  deployed: 'Deployed',
  pending_infrastructure: 'Waiting for a Greyspace host',
  failed: 'Failed',
};

/**
 * Greyspace settings for one range (ADR 0007, mockup assets/previews/greyspace.html):
 * corpus tier, site packs, public address space, simulated users, threat-actor
 * infrastructure. Used in the Range Designer's side panel and as its own page
 * (/authoring/ranges/:rangeId/greyspace).
 */
@Component({
  selector: 'tn-greyspace-panel',
  imports: [
    FormsModule,
    RouterLink,
    MatButtonModule,
    MatCheckboxModule,
    MatFormFieldModule,
    MatIconModule,
    MatInputModule,
    MatSelectModule,
    MatSlideToggleModule,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="gs" aria-labelledby="gs-title">
      <header class="gs-head">
        <h3 id="gs-title"><mat-icon>public</mat-icon> Greyspace internet</h3>
        @if (status(); as s) {
          <span class="pill" [class.ok]="s.status === 'deployed' || s.status === 'configured'"
                [class.warn]="s.status === 'pending_infrastructure' || s.status === 'failed'"
                data-test="gs-status">{{ statusLabel() }}</span>
        }
      </header>
      <p class="hint">Backbone routing, root DNS, rehosted sites and attacker infrastructure. Site
        content is read from one shared corpus; breadcrumbs go in this range's own overlay.</p>

      @if (error()) {
        <p class="error" role="alert">{{ error() }}</p>
      }

      @if (status(); as s) {
        @if (!s.attached && s.template_block) {
          <p class="hint">This range's template declares a Greyspace block.</p>
        }

        <mat-form-field appearance="outline" class="full">
          <mat-label>Corpus</mat-label>
          <mat-select [(ngModel)]="draft.corpus_tier" (selectionChange)="onTier()" name="tier">
            @for (c of corpora(); track c.tier) {
              <mat-option [value]="c.tier">{{ c.title }}{{ c.sites ? ' · ' + c.sites + ' sites' : '' }}</mat-option>
            }
          </mat-select>
        </mat-form-field>

        @if (packs().length) {
          <fieldset class="packs">
            <legend>Site packs</legend>
            @for (p of packs(); track p.name) {
              <mat-checkbox [(ngModel)]="draft.packs[p.name]" [name]="'pack-' + p.name">
                {{ p.name }} <small>{{ p.count }}</small>
              </mat-checkbox>
            }
          </fieldset>
        } @else {
          <p class="hint">The control plane cannot read this tier's manifest; every pack is served.</p>
        }

        <mat-form-field appearance="outline" class="full">
          <mat-label>Public address space</mat-label>
          <input matInput [(ngModel)]="draft.public_prefix" name="prefix" placeholder="from the corpus (e.g. 198.18.0.0/15)">
        </mat-form-field>

        <mat-form-field appearance="outline" class="full">
          <mat-label>Simulated users</mat-label>
          <mat-select [(ngModel)]="draft.npc_profile" name="npc">
            <mat-option value="off">Off</mat-option>
            <mat-option value="office-day">Office day</mat-option>
            <mat-option value="quiet-night">Quiet night</mat-option>
          </mat-select>
          <mat-hint>Recorded now; simulated users arrive in a later release.</mat-hint>
        </mat-form-field>

        <mat-slide-toggle [(ngModel)]="draft.threat_infra" name="threat">Threat-actor infrastructure</mat-slide-toggle>
        <mat-slide-toggle [(ngModel)]="draft.trust_ca" name="ca">Trust the Greyspace root CA</mat-slide-toggle>

        @if (s.problems?.length) {
          <ul class="error">
            @for (p of s.problems ?? []; track p) { <li>{{ p }}</li> }
          </ul>
        }
        @if (s.detail?.['note'] || s.detail?.['todo']) {
          <p class="hint">{{ s.detail?.['note'] || s.detail?.['todo'] }}</p>
        }

        <div class="actions">
          <button mat-flat-button color="primary" (click)="save()" [disabled]="busy()" data-test="gs-save">
            <mat-icon>save</mat-icon> {{ s.attached ? 'Save' : 'Attach Greyspace' }}
          </button>
          @if (s.attached) {
            <button mat-stroked-button (click)="detach()" [disabled]="busy()" data-test="gs-detach">
              <mat-icon>link_off</mat-icon> Detach
            </button>
          }
          @if (showPageLink()) {
            <a mat-button [routerLink]="['/authoring/ranges', rangeId(), 'greyspace']">Open page</a>
          }
        </div>

        @if (config(); as c) {
          <dl class="summary" data-test="gs-config">
            <dt>Services</dt><dd>{{ c.services.join(', ') }}</dd>
            <dt>Sites</dt><dd>{{ c.sites }} in {{ c.zones.length }} DNS zones under {{ c.tlds.length }} TLDs</dd>
            <dt>Resolver</dt><dd><code>{{ c.address_plan['resolver'] }}</code></dd>
            <dt>ISPs</dt>
            <dd>@for (i of c.isps; track i['asn']) { <span>AS{{ i['asn'] }} {{ i['prefix'] }}</span> }</dd>
            @if (c.threat_domains.length) {
              <dt>Threat domains</dt>
              <dd>@for (t of c.threat_domains; track t['fqdn']) { <span>{{ t['fqdn'] }} ({{ t['role'] }})</span> }</dd>
            }
          </dl>
        }
      } @else if (!error()) {
        <p class="hint">Loading…</p>
      }
    </section>
  `,
  styles: [`
    .gs { display: flex; flex-direction: column; gap: 8px; }
    .gs-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .gs-head h3 { display: flex; align-items: center; gap: 6px; margin: 0; font-size: 15px; }
    .pill { font-size: 12px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border); }
    .pill.ok { border-color: var(--success, #2e7d32); color: var(--success, #2e7d32); }
    .pill.warn { border-color: var(--warning, #b26a00); color: var(--warning, #b26a00); }
    .hint { color: var(--text-secondary); font-size: 13px; margin: 0; }
    .error { color: var(--error, #b00020); font-size: 13px; margin: 0; }
    .full { width: 100%; }
    .packs { border: 1px solid var(--border); border-radius: 8px; padding: 4px 8px; display: flex; flex-wrap: wrap; gap: 0 12px; }
    .packs legend { font-size: 12px; color: var(--text-secondary); }
    .packs small { color: var(--text-secondary); }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 4px; }
    .summary { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; font-size: 13px; margin: 8px 0 0; }
    .summary dt { color: var(--text-secondary); }
    .summary dd { margin: 0; display: flex; flex-wrap: wrap; gap: 4px 10px; }
  `],
})
export class GreyspacePanelComponent {
  private readonly api = inject(GreyspaceApiService);
  private readonly notify = inject(NotificationService);

  /** The range; bound from the route parameter on the Greyspace page. */
  readonly rangeId = input.required<string>();
  /** Show a link to the full page (in the designer's side panel). */
  readonly showPageLink = input(false);

  readonly status = signal<GreyspaceStatus | null>(null);
  readonly config = signal<GreyspaceConfig | null>(null);
  readonly corpora = signal<GreyspaceCorpus[]>([]);
  readonly busy = signal(false);
  readonly error = signal('');
  private readonly tier = signal<Tier>('t0');

  draft: Draft = this.emptyDraft();

  readonly statusLabel = computed(() => STATUS_LABEL[this.status()?.status ?? ''] ?? this.status()?.status ?? '');
  readonly packs = computed(() => {
    const corpus = this.corpora().find(c => c.tier === this.tier());
    return Object.entries(corpus?.categories ?? {}).map(([name, count]) => ({ name, count }));
  });

  constructor() {
    this.api.corpora().subscribe({
      next: c => {
        this.corpora.set(c);
        const s = this.status();
        this.fillPacks((s?.block ?? s?.template_block)?.site_packs);
      },
      error: () => this.corpora.set([]),
    });
    effect(() => this.load(this.rangeId()));
  }

  onTier(): void {
    this.tier.set(this.draft.corpus_tier);
    this.draft.packs = {};
    this.fillPacks(null);
  }

  save(): void {
    this.busy.set(true);
    this.error.set('');
    this.api.attach(this.rangeId(), this.toBlock()).subscribe({
      next: s => {
        this.busy.set(false);
        this.apply(s);
        this.notify.success('Greyspace saved');
      },
      error: err => {
        this.busy.set(false);
        const detail = err?.error?.detail;
        const problems: string[] = detail?.problems ?? [];
        this.error.set(problems.length ? problems.join('; ') : typeof detail === 'string' ? detail : 'Could not save Greyspace');
      },
    });
  }

  detach(): void {
    this.busy.set(true);
    this.error.set('');
    this.api.detach(this.rangeId()).subscribe({
      next: () => {
        this.busy.set(false);
        this.notify.success('Greyspace detached');
        this.load(this.rangeId());
      },
      error: err => {
        this.busy.set(false);
        this.error.set(typeof err?.error?.detail === 'string' ? err.error.detail : 'Could not detach Greyspace');
      },
    });
  }

  /** The block to send: unticked packs are left out; all ticked (or none known) means every pack. */
  toBlock(): Partial<GreyspaceBlock> {
    const known = this.packs().map(p => p.name);
    const chosen = known.filter(n => this.draft.packs[n]);
    return {
      version: 1,
      corpus_tier: this.draft.corpus_tier,
      site_packs: known.length && chosen.length < known.length ? chosen : null,
      public_prefix: this.draft.public_prefix.trim() || null,
      npc_profile: this.draft.npc_profile,
      threat_infra: this.draft.threat_infra,
      trust_ca: this.draft.trust_ca,
    };
  }

  private load(rangeId: string): void {
    this.error.set('');
    this.api.status(rangeId).subscribe({
      next: s => this.apply(s),
      error: err => this.error.set(err?.status === 404 ? 'Range not found' : 'Could not load Greyspace'),
    });
  }

  private apply(s: GreyspaceStatus): void {
    this.status.set(s);
    const block = s.block ?? s.template_block ?? null;
    this.draft = this.emptyDraft(block);
    this.tier.set(this.draft.corpus_tier);
    this.fillPacks(block?.site_packs);
    this.config.set(null);
    if (s.attached && s.corpus?.available) {
      this.api.config(this.rangeId()).subscribe({ next: c => this.config.set(c), error: () => this.config.set(null) });
    }
  }

  /** Tick each known pack the draft has no answer for yet: on when the block serves every pack or names it. */
  private fillPacks(sitePacks: string[] | null | undefined): void {
    for (const { name } of this.packs()) {
      if (this.draft.packs[name] === undefined) {
        this.draft.packs[name] = !sitePacks || sitePacks.includes(name);
      }
    }
  }

  private emptyDraft(block: GreyspaceBlock | null = null): Draft {
    return {
      corpus_tier: (block?.corpus_tier ?? 't0') as Tier,
      packs: {},
      public_prefix: block?.public_prefix ?? '',
      npc_profile: (block?.npc_profile ?? 'off') as Npc,
      threat_infra: block?.threat_infra ?? true,
      trust_ca: block?.trust_ca ?? true,
    };
  }
}
