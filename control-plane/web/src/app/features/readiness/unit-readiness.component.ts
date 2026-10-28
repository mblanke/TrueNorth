import { Component, OnInit, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatSelectModule } from '@angular/material/select';
import {
  ReadinessApiService, ReadinessFilter, ReadinessStudent, UnitReadiness,
} from '@core/services/readiness-api.service';
import { NotificationService } from '@core/services/notification.service';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';
import { apiError } from '../ranges/range-capabilities';

const RESULT_LABEL: Record<string, string> = { pass: 'Pass', fail: 'Fail', incomplete: 'Incomplete' };

/** A close-out result in words; none yet is "Awaiting result". Never a score. */
export function resultText(outcome: string | null | undefined): string {
  return outcome ? (RESULT_LABEL[outcome] ?? outcome) : 'Awaiting result';
}

/** Recorded attendance as counts, never a percentage. */
export function attendanceText(s: Pick<ReadinessStudent, 'present' | 'excused' | 'absent'>): string {
  if (!s.present && !s.excused && !s.absent) return 'None recorded';
  return `${s.present} present, ${s.excused} excused, ${s.absent} absent`;
}

function save(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

/**
 * The unit readiness page (ADR 0014 decision 7): commander = observer + this page. Every
 * class of the organisation in a date range, with its Students' enrolment, recorded
 * attendance, result (pass, fail or incomplete, with the instructor's comment) and the
 * qualification a pass earned. Filtered by unit and date; exported as CSV or PDF. Read-only:
 * nothing here changes a record.
 */
@Component({
  selector: 'tn-unit-readiness',
  imports: [
    DatePipe, FormsModule, MatButtonModule, MatCardModule, MatChipsModule, MatFormFieldModule, MatIconModule,
    MatInputModule, MatProgressBarModule, MatSelectModule, EmptyStateComponent,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">military_tech</mat-icon>
          <div>
            <h1>Unit readiness</h1>
            <p class="subtitle">Classes, enrolment, attendance, results and qualifications earned. Read-only.</p>
          </div>
        </div>
        <div class="actions">
          <button mat-stroked-button (click)="exportCsv()"><mat-icon>download</mat-icon> CSV</button>
          <button mat-stroked-button (click)="exportPdf()"><mat-icon>picture_as_pdf</mat-icon> PDF</button>
          <button mat-stroked-button (click)="print()"><mat-icon>print</mat-icon> Print</button>
        </div>
      </div>

      <div class="filters">
        <mat-form-field appearance="outline">
          <mat-label>From</mat-label>
          <input matInput type="date" [(ngModel)]="filter.dateFrom" (change)="load()" />
        </mat-form-field>
        <mat-form-field appearance="outline">
          <mat-label>To</mat-label>
          <input matInput type="date" [(ngModel)]="filter.dateTo" (change)="load()" />
        </mat-form-field>
        <mat-form-field appearance="outline" class="unit">
          <mat-label>Unit</mat-label>
          <mat-select [(ngModel)]="filter.unit" (selectionChange)="load()">
            <mat-option value="">All units</mat-option>
            @for (u of report()?.units ?? []; track u) { <mat-option [value]="u">{{ u }}</mat-option> }
          </mat-select>
        </mat-form-field>
      </div>

      @if (loading()) { <mat-progress-bar mode="indeterminate" /> }

      @if (report(); as r) {
        <p class="period">
          {{ r.date_from | date:'d MMM y' }} to {{ r.date_to | date:'d MMM y' }}, {{ r.unit || 'all units' }}
        </p>
        <div class="summary">
          <mat-card appearance="outlined" class="tile">
            <span class="label">Classes</span><span class="value">{{ r.summary.classes }}</span>
            <span class="small">{{ r.summary.sessions_held }} sessions held</span>
          </mat-card>
          <mat-card appearance="outlined" class="tile">
            <span class="label">Students</span><span class="value">{{ r.summary.students }}</span>
            <span class="small">{{ r.summary.enrolments }} places</span>
          </mat-card>
          <mat-card appearance="outlined" class="tile">
            <span class="label">Attendance</span>
            <span class="small">{{ r.summary.present }} present, {{ r.summary.excused }} excused,
              {{ r.summary.absent }} absent</span>
          </mat-card>
          <mat-card appearance="outlined" class="tile">
            <span class="label">Results</span>
            <span class="small">{{ r.summary.passed }} pass, {{ r.summary.failed }} fail,
              {{ r.summary.incomplete }} incomplete, {{ r.summary.awaiting_result }} awaiting</span>
          </mat-card>
          <mat-card appearance="outlined" class="tile">
            <span class="label">Qualifications</span>
            <span class="value">{{ r.summary.qualifications_earned }}</span>
            <span class="small">{{ r.summary.qualifications_earned }} earned</span>
          </mat-card>
        </div>
        @if (r.qualifications.length) {
          <mat-chip-set class="quals" aria-label="Qualifications earned">
            @for (q of r.qualifications; track q.qualification_id) {
              <mat-chip>{{ q.code }}: {{ q.title }} ({{ q.earned }})</mat-chip>
            }
          </mat-chip-set>
        }

        @if (r.classes.length === 0) {
          <tn-empty-state icon="event_busy" title="No classes"
            [message]="r.unit ? 'No class in this date range has Students of ' + r.unit + '.' : 'No class in this date range.'" />
        }
        @for (c of r.classes; track c.class_id) {
          <mat-card appearance="outlined" class="class-card">
            <mat-card-header>
              <mat-card-title>{{ c.name }}</mat-card-title>
              <mat-card-subtitle>
                {{ c.course_name }}@if (c.qualification) { , delivers {{ c.qualification }} }
                · {{ c.start_date | date:'d MMM' }} to {{ c.end_date | date:'d MMM y' }}
                · <span [class]="'status status-' + c.status">{{ c.status }}</span>
                · {{ c.sessions_held }} sessions held
              </mat-card-subtitle>
            </mat-card-header>
            <mat-card-content>
              <p class="small">{{ c.enrolled }} Students: {{ c.passed }} pass, {{ c.failed }} fail,
                {{ c.incomplete }} incomplete, {{ c.awaiting_result }} awaiting</p>
              @if (c.students.length) {
                <div class="scroll">
                  <table class="results">
                    <thead>
                      <tr><th>Student</th><th>Unit</th><th>Enrolment</th><th>Attendance</th><th>Result</th>
                        <th>Instructor's comment</th><th>Qualification</th></tr>
                    </thead>
                    <tbody>
                      @for (s of c.students; track s.entry_id) {
                        <tr class="student-row">
                          <td>{{ s.display_name || s.email }}</td>
                          <td>{{ s.unit || '-' }}</td>
                          <td>{{ s.enrolment_status || s.roll_call_status }}</td>
                          <td>{{ attendance(s) }}</td>
                          <td [class]="'outcome-' + (s.outcome ?? 'none')">{{ result(s.outcome) }}</td>
                          <td class="comment">{{ s.comment || '' }}</td>
                          <td>{{ s.qualification_earned || '' }}</td>
                        </tr>
                      }
                    </tbody>
                  </table>
                </div>
              }
            </mat-card-content>
          </mat-card>
        }
      }
    </div>
  `,
  styles: [`
    .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .filters { display: flex; gap: 0 16px; flex-wrap: wrap; align-items: center; }
    .filters .unit { min-width: 220px; }
    .period { margin: 0 0 8px; color: var(--mat-sys-on-surface-variant); }
    .summary { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px; margin-bottom: 12px; }
    .tile { padding: 12px 16px; display: flex; flex-direction: column; gap: 2px; }
    .tile .label { font: var(--mat-sys-label-medium); color: var(--mat-sys-on-surface-variant); }
    .tile .value { font: var(--mat-sys-headline-small); }
    .quals { margin-bottom: 12px; display: block; }
    .class-card { margin: 16px 0; }
    .scroll { overflow-x: auto; }
    .results { width: 100%; border-collapse: collapse; margin-top: 8px; }
    .results th, .results td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--mat-sys-outline-variant); vertical-align: top; }
    .results th { font: var(--mat-sys-label-large); }
    .comment { max-width: 320px; white-space: pre-wrap; }
    .outcome-pass { color: var(--mat-sys-primary); font-weight: 600; }
    .outcome-fail { color: var(--mat-sys-error); font-weight: 600; }
    .outcome-none { color: var(--mat-sys-on-surface-variant); }
    .status { text-transform: capitalize; }
    .small { font-size: 12px; opacity: 0.8; }
    @media print {
      .actions, .filters, mat-progress-bar { display: none !important; }
      .class-card { break-inside: avoid; box-shadow: none; }
      .scroll { overflow: visible; }
      .results th, .results td { padding: 3px 4px; font-size: 10px; color: #000; }
    }
  `],
})
export class UnitReadinessComponent implements OnInit {
  private readonly api = inject(ReadinessApiService);
  private readonly notify = inject(NotificationService);

  readonly report = signal<UnitReadiness | null>(null);
  readonly loading = signal(false);
  filter: ReadinessFilter = { dateFrom: '', dateTo: '', unit: '' };

  readonly result = resultText;
  readonly attendance = attendanceText;

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.api.get({ ...this.filter }).subscribe({
      next: r => { this.report.set(r); this.loading.set(false); },
      error: err => { this.loading.set(false); this.notify.error(apiError(err)); },
    });
  }

  private fileName(ext: string): string {
    const unit = (this.filter.unit || 'all').replace(/[^A-Za-z0-9_-]/g, '_');
    return `readiness-${unit}.${ext}`;
  }

  exportCsv(): void {
    this.api.exportCsv({ ...this.filter }).subscribe({
      next: text => save(new Blob([text], { type: 'text/csv;charset=utf-8' }), this.fileName('csv')),
      error: err => this.notify.error(apiError(err)),
    });
  }

  exportPdf(): void {
    this.api.exportPdf({ ...this.filter }).subscribe({
      next: blob => save(blob, this.fileName('pdf')),
      error: err => this.notify.error(apiError(err)),
    });
  }

  print(): void {
    window.print();
  }
}
