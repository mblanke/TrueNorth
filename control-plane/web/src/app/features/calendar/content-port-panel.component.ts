import { Component, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpErrorResponse } from '@angular/common/http';
import { MatButtonModule } from '@angular/material/button';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { MatIconModule } from '@angular/material/icon';
import {
  ContentPortApiService, PortItemType, PortPreview, PortSigningKey, ProvenStatus,
} from '@core/services/content-port-api.service';
import { NotificationService } from '@core/services/notification.service';
import { apiError } from '../ranges/range-capabilities';

const TYPE_LABEL: Record<string, string> = { course: 'Course', range_template: 'Range template', scenario: 'Scenario' };
const ACTION_LABEL: Record<string, string> = {
  create: 'New', update: 'Update', unchanged: 'Unchanged', conflict: 'Conflict', refused: 'Refused',
  skipped: 'Skipped',
};

/** One item the export refused, as the API's 409 lists it. */
export interface ExportProblem {
  item_type: string;
  item_id: string;
  title: string | null;
  dependency?: boolean;
  reasons: string[];
}

/** The refusal of an export: the API's list of what is not proven, or its message. */
export function exportProblems(err: unknown): { message: string; items: ExportProblem[] } {
  if (err instanceof HttpErrorResponse && err.status === 409) {
    let body: unknown = err.error;
    if (typeof body === 'string') {
      try { body = JSON.parse(body); } catch { body = null; }
    }
    const detail = (body as { detail?: unknown } | null)?.detail;
    if (detail && typeof detail === 'object' && Array.isArray((detail as { items?: unknown }).items)) {
      const d = detail as { message?: string; items: ExportProblem[] };
      return { message: d.message ?? 'Only proven content is exported.', items: d.items };
    }
    if (typeof detail === 'string') return { message: detail, items: [] };
  }
  return { message: apiError(err), items: [] };
}

/**
 * The Content view's Port content panel (docs/content-port.md), for the platform
 * administrator: export proven courses, range templates and scenarios as a signed bundle
 * (what they need comes along, and must be proven too), and preview and import a bundle from
 * another site. Imported items land not yet proven here.
 */
@Component({
  selector: 'tn-content-port-panel',
  imports: [DatePipe, FormsModule, MatButtonModule, MatCheckboxModule, MatIconModule],
  template: `
    <section class="card" aria-label="Port content">
      <div class="card-head"><h2>Port content</h2></div>
      <p class="muted">Only proven content moves to another site, and it lands there not yet proven.</p>

      <div class="sub">Export</div>
      @if (!items()) {
        <button mat-stroked-button (click)="loadItems()" [disabled]="busy()">
          <mat-icon>inventory_2</mat-icon> Choose content to export
        </button>
      } @else {
        <div class="list" role="group" aria-label="Content to export">
          @for (it of items(); track it.item_type + it.item_id) {
            <div class="item">
              <mat-checkbox [checked]="isPicked(it)" [disabled]="!it.proven" (change)="toggle(it)">
                <span class="type">{{ typeLabel(it.item_type) }}</span> {{ it.title }}
              </mat-checkbox>
              @if (!it.proven) { <div class="why">{{ it.label }}</div> }
            </div>
          } @empty {
            <p class="muted">No content here.</p>
          }
        </div>
        <button mat-flat-button (click)="exportPicked()" [disabled]="busy() || !picked().size">
          <mat-icon>download</mat-icon> Export proven content
        </button>
      }
      @if (refusal(); as r) {
        <div class="refusal" role="alert">
          <div>{{ r.message }}</div>
          @for (p of r.items; track p.item_type + p.item_id) {
            <div class="problem">{{ typeLabel(p.item_type) }} {{ p.title ?? p.item_id }}
              @if (p.dependency) { <span class="dep">(needed)</span> }: {{ p.reasons.join('; ') }}</div>
          }
        </div>
      }

      <div class="sub">Import</div>
      <div class="shared"><mat-checkbox [(ngModel)]="shared">Land as shared content</mat-checkbox></div>
      <input #picker type="file" accept=".json,application/json" hidden (change)="pickedFile(picker)">
      <button mat-stroked-button (click)="picker.click()" [disabled]="busy()">
        <mat-icon>upload_file</mat-icon> Preview a bundle
      </button>
      @if (preview(); as pv) {
        <div class="preview" aria-label="Import preview">
          <div class="meta">Bundle made {{ pv.exported_at | date: 'd MMM y, HH:mm' }} · key {{ pv.key_id }}</div>
          @if (pv.already_imported_at) {
            <p class="err">Already imported on {{ pv.already_imported_at | date: 'd MMM y' }}; export a new bundle.</p>
          }
          @for (i of pv.items; track i.key) {
            <div class="pitem" [class]="'pitem ' + i.action">
              <div class="phead">
                <span class="action">{{ actionLabel(i.action) }}</span>
                <span class="type">{{ typeLabel(i.item_type) }}</span> {{ i.name }}
              </div>
              @for (r of i.reasons ?? []; track $index) { <div class="reason">{{ r }}</div> }
              @for (n of i.notes ?? []; track $index) { <div class="note">{{ n }}</div> }
              @if (i.action === 'conflict') {
                <mat-checkbox [checked]="overwrite().has(i.key)" (change)="toggleOverwrite(i.key)">
                  Replace what is here
                </mat-checkbox>
              }
              @if (i.diff) {
                <details><summary>Diff</summary><pre>{{ i.diff }}</pre></details>
              }
            </div>
          }
          @for (b of pv.missing_golden_builds ?? []; track b.catalogue_id + b.build_id) {
            <div class="missing">Port this golden template first: {{ b.catalogue_id }} build {{ b.build_id }}</div>
          }
          <div class="actions">
            <button mat-flat-button (click)="importNow()" [disabled]="busy() || !pv.can_import">Import</button>
            <button mat-button (click)="clearPreview()">Cancel</button>
          </div>
        </div>
      }

      <div class="sub">This site's key</div>
      @if (key(); as k) {
        <p class="muted">Key {{ k.key_id }}. The importing site trusts it in <code>tn_content_port_trusted_keys</code>:</p>
        <code class="pub">{{ k.public_key }}</code>
      } @else {
        <button mat-button (click)="loadKey()">Show</button>
      }
    </section>
  `,
  styles: [`
    .card { background: var(--mat-sys-surface); border-radius: 18px; padding: 18px; display: flex; flex-direction: column;
      gap: 10px; box-shadow: 0 1px 2px rgba(17, 24, 39, 0.05), 0 8px 24px rgba(17, 24, 39, 0.05); }
    .card-head h2 { margin: 0; font-size: 13px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
      color: var(--mat-sys-on-surface-variant); }
    .sub { font-size: 12px; font-weight: 700; color: var(--mat-sys-on-surface); margin-top: 6px; }
    .muted { color: var(--mat-sys-on-surface-variant); font-size: 13px; margin: 0; }
    .list { display: flex; flex-direction: column; gap: 4px; max-height: 280px; overflow-y: auto; }
    .item { display: flex; flex-direction: column; font-size: 13px; }
    .type { font-size: 11px; font-weight: 600; color: var(--mat-sys-on-surface-variant); }
    .why { font-size: 11px; color: #B54708; padding-left: 40px; overflow-wrap: anywhere; }
    .refusal { font-size: 12px; color: #B42318; display: flex; flex-direction: column; gap: 4px; }
    .problem { overflow-wrap: anywhere; }
    .dep { font-weight: 600; }
    .preview { display: flex; flex-direction: column; gap: 8px; }
    .meta { font-size: 12px; color: var(--mat-sys-on-surface-variant); }
    .pitem { padding: 10px 12px; border-radius: 12px; background: var(--mat-sys-surface-container-low); font-size: 13px;
      display: flex; flex-direction: column; gap: 4px; }
    .pitem.conflict { outline: 1.5px solid #F79009; outline-offset: -1.5px; }
    .pitem.refused, .pitem.skipped { outline: 1.5px solid #F04438; outline-offset: -1.5px; }
    .action { font-size: 11px; font-weight: 700; border-radius: 8px; padding: 1px 8px; margin-right: 6px;
      background: var(--mat-sys-surface-container-high); }
    .reason { font-size: 12px; color: #B42318; }
    .note { font-size: 12px; color: var(--mat-sys-on-surface-variant); }
    .missing { font-size: 12px; color: #B54708; }
    pre { font-size: 11px; max-height: 240px; overflow: auto; margin: 4px 0 0; white-space: pre; }
    .actions { display: flex; gap: 8px; }
    .err { font-size: 12px; color: #B42318; margin: 0; }
    .pub { font-size: 11px; overflow-wrap: anywhere; user-select: all; }
    code { font-size: 12px; }
  `],
})
export class ContentPortPanelComponent {
  private readonly api = inject(ContentPortApiService);
  private readonly notify = inject(NotificationService);

  readonly items = signal<ProvenStatus[] | null>(null);
  readonly picked = signal<ReadonlySet<string>>(new Set());
  readonly refusal = signal<{ message: string; items: ExportProblem[] } | null>(null);
  readonly preview = signal<PortPreview | null>(null);
  readonly overwrite = signal<ReadonlySet<string>>(new Set());
  readonly key = signal<PortSigningKey | null>(null);
  readonly busy = signal(false);
  shared = false;
  private file: File | null = null;

  typeLabel(type: string): string {
    return TYPE_LABEL[type] ?? type;
  }
  actionLabel(action: string): string {
    return ACTION_LABEL[action] ?? action;
  }
  private keyOf(it: Pick<ProvenStatus, 'item_type' | 'item_id'>): string {
    return `${it.item_type}:${it.item_id}`;
  }
  isPicked(it: ProvenStatus): boolean {
    return this.picked().has(this.keyOf(it));
  }

  loadItems(): void {
    this.busy.set(true);
    this.api.items().subscribe({
      next: list => { this.busy.set(false); this.items.set(list); },
      error: e => { this.busy.set(false); this.notify.error(apiError(e)); },
    });
  }

  loadKey(): void {
    this.api.signingKey().subscribe({ next: k => this.key.set(k), error: e => this.notify.error(apiError(e)) });
  }

  toggle(it: ProvenStatus): void {
    const next = new Set(this.picked());
    const k = this.keyOf(it);
    if (next.has(k)) next.delete(k);
    else next.add(k);
    this.picked.set(next);
  }

  exportPicked(): void {
    const items = [...this.picked()].map(k => {
      const [item_type, item_id] = k.split(':');
      return { item_type: item_type as PortItemType, item_id };
    });
    this.busy.set(true);
    this.refusal.set(null);
    this.api.exportBundle(items).subscribe({
      next: text => {
        this.busy.set(false);
        let id = 'bundle';
        try { id = String(JSON.parse(text).bundle_id ?? id); } catch { /* the file is what the API sent */ }
        const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
        const a = document.createElement('a');
        a.href = url;
        a.download = `truenorth-content-${id.replace(/[^A-Za-z0-9-]/g, '')}.json`;
        a.click();
        URL.revokeObjectURL(url);
        this.notify.success('Signed bundle exported');
      },
      error: e => {
        this.busy.set(false);
        this.refusal.set(exportProblems(e));
      },
    });
  }

  pickedFile(input: HTMLInputElement): void {
    const file = input.files?.[0];
    input.value = '';
    if (!file) return;
    this.file = file;
    this.overwrite.set(new Set());
    this.runPreview();
  }

  private runPreview(): void {
    if (!this.file) return;
    this.busy.set(true);
    this.api.preview(this.file, { shared: this.shared }).subscribe({
      next: pv => { this.busy.set(false); this.preview.set(pv); },
      error: e => { this.busy.set(false); this.preview.set(null); this.notify.error(apiError(e)); },
    });
  }

  toggleOverwrite(key: string): void {
    const next = new Set(this.overwrite());
    if (next.has(key)) next.delete(key);
    else next.add(key);
    this.overwrite.set(next);
  }

  clearPreview(): void {
    this.preview.set(null);
    this.file = null;
    this.overwrite.set(new Set());
  }

  importNow(): void {
    if (!this.file) return;
    this.busy.set(true);
    this.api.importBundle(this.file, { shared: this.shared }, [...this.overwrite()]).subscribe({
      next: res => {
        this.busy.set(false);
        this.notify.success(`Imported: ${res.created} new, ${res.updated} updated, ${res.unchanged} unchanged, `
          + `${res.skipped} skipped. None is proven here yet.`);
        this.preview.set(null);
        this.file = null;
      },
      error: e => { this.busy.set(false); this.notify.error(apiError(e)); },
    });
  }
}
