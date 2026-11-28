// Author: Marc-Andre Blanke
// Created: 2026-11-27T00:00:00Z
import { Component, OnDestroy, OnInit, inject, signal } from '@angular/core';
import { Subscription } from 'rxjs';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatSelectModule } from '@angular/material/select';
import {
  StudentFilter, StudentProgressApiService, StudentRecord, StudentRow,
} from '@core/services/student-progress-api.service';
import { NotificationService } from '@core/services/notification.service';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';
import { apiError } from '../ranges/range-capabilities';
import { attendanceText, resultText } from '../readiness/unit-readiness.component';

/** A NICE level name for people: "Intermediate"; none recorded is a dash. Never a score. */
export function levelText(level: string | null | undefined): string {
  return level ? level.charAt(0).toUpperCase() + level.slice(1) : '-';
}

/** Attendance for the table, as counts: "3 of 4 present" (", 1 excused"). Never a percentage. */
export function attendanceShort(r: Pick<StudentRow, 'present' | 'excused' | 'absent'>): string {
  const total = r.present + r.excused + r.absent;
  if (!total) return 'None recorded';
  return `${r.present} of ${total} present` + (r.excused ? `, ${r.excused} excused` : '');
}

/** A Student's results across their classes, in words: "1 pass, 1 awaiting". */
export function resultsText(r: Pick<StudentRow, 'passed' | 'failed' | 'incomplete' | 'awaiting_result'>): string {
  const parts = [
    [r.passed, 'pass'], [r.failed, 'fail'], [r.incomplete, 'incomplete'], [r.awaiting_result, 'awaiting'],
  ].filter(([n]) => (n as number) > 0).map(([n, w]) => `${n} ${w}`);
  return parts.length ? parts.join(', ') : 'No classes';
}

/**
 * The Students page: every Student of the organisation with their formation, proficiency,
 * attendance, results and qualifications; select one for their record (competency by NICE
 * category, and every class with attendance, result and the instructor's comment). For
 * instructors and admins. Read-only, and no score, percentage or grade (ADR 0014).
 */
@Component({
  selector: 'tn-student-progress',
  imports: [
    DatePipe, FormsModule, MatButtonModule, MatCardModule, MatFormFieldModule, MatIconModule, MatInputModule,
    MatProgressBarModule, MatSelectModule, EmptyStateComponent,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">school</mat-icon>
          <div>
            <h1>Students</h1>
            <p class="subtitle">Every Student's formation, proficiency, attendance, results and qualifications.
              Select a Student for their record. Read-only.</p>
          </div>
        </div>
      </div>

      <div class="filters">
        <mat-form-field appearance="outline" class="unit">
          <mat-label>Formation</mat-label>
          <mat-select [(ngModel)]="filter.unit" (selectionChange)="load()">
            <mat-option value="">All formations</mat-option>
            @for (u of units(); track u) { <mat-option [value]="u">{{ u }}</mat-option> }
          </mat-select>
        </mat-form-field>
        <mat-form-field appearance="outline" class="search">
          <mat-label>Search</mat-label>
          <input matInput [(ngModel)]="filter.q" (keyup.enter)="load()" placeholder="Name, email or callsign" />
          <button mat-icon-button matSuffix (click)="load()" aria-label="Search"><mat-icon>search</mat-icon></button>
        </mat-form-field>
        <span class="count">{{ rows().length }} Students</span>
      </div>

      @if (loading()) { <mat-progress-bar mode="indeterminate" /> }

      <div class="layout" [class.with-record]="!!selected()">
        <div class="scroll list">
          @if (!loading() && rows().length === 0) {
            <tn-empty-state icon="person_search" title="No Students" message="No Student matches these filters." />
          } @else {
            <table class="results">
              <thead>
                <tr><th>Rank</th><th>Name</th><th>Formation</th><th>Proficiency</th><th>Attendance</th>
                  <th>Results</th><th>Qualifications</th></tr>
              </thead>
              <tbody>
                @for (s of rows(); track s.user_id) {
                  <tr class="student-row" [class.active]="selected()?.user_id === s.user_id" tabindex="0"
                      (click)="open(s)" (keyup.enter)="open(s)">
                    <td>{{ s.rank || '' }}</td>
                    <td class="name">{{ s.display_name }}</td>
                    <td>{{ s.unit || '-' }}</td>
                    <td [class]="'level level-' + (s.proficiency ?? 'none')">{{ level(s.proficiency) }}</td>
                    <td class="nowrap">{{ attendanceShort(s) }}</td>
                    <td>{{ results(s) }}</td>
                    <td>{{ s.qualifications.join(', ') }}</td>
                  </tr>
                }
              </tbody>
            </table>
          }
        </div>

        @if (selected(); as r) {
          <mat-card appearance="outlined" class="record" aria-live="polite">
            <mat-card-header>
              <mat-card-title>{{ r.display_name }}</mat-card-title>
              <mat-card-subtitle>
                {{ r.unit || 'No formation' }}@if (r.callsign) { · {{ r.callsign }} } · {{ r.email }}
              </mat-card-subtitle>
              <button mat-icon-button class="close" (click)="selected.set(null)" aria-label="Close the record">
                <mat-icon>close</mat-icon>
              </button>
            </mat-card-header>
            <mat-card-content>
              <h3>Proficiency: {{ level(r.proficiency) }}</h3>
              @if (r.competencies.length) {
                <table class="results compact">
                  <thead><tr><th>NICE category</th><th>Level</th><th>Ratings</th></tr></thead>
                  <tbody>
                    @for (c of r.competencies; track c.category) {
                      <tr><td>{{ c.category }}</td>
                        <td [class]="'level level-' + c.proficiency">{{ level(c.proficiency) }}</td>
                        <td>{{ c.assertions }}</td></tr>
                    }
                  </tbody>
                </table>
              } @else {
                <p class="small">No competency recorded.</p>
              }

              <h3>Classes</h3>
              @if (r.class_history.length === 0) { <p class="small">Not on any class.</p> }
              @for (c of r.class_history; track c.class_id) {
                <div class="class">
                  <div class="class-head">
                    <strong>{{ c.name }}</strong>
                    <span [class]="'outcome-' + (c.outcome ?? 'none')">{{ result(c.outcome) }}</span>
                  </div>
                  <div class="small">
                    {{ c.course_name }} · {{ c.start_date | date:'d MMM' }} to {{ c.end_date | date:'d MMM y' }}
                    · <span class="status">{{ c.status }}</span> · {{ c.sessions_held }} sessions held
                  </div>
                  <div class="small">{{ attendance(c) }}
                    @if (c.qualification_earned) { · earned {{ c.qualification_earned }} }</div>
                  @if (c.comment) { <div class="comment">"{{ c.comment }}"</div> }
                </div>
              }
            </mat-card-content>
          </mat-card>
        }
      </div>
    </div>
  `,
  styles: [`
    .filters { display: flex; gap: 0 16px; flex-wrap: wrap; align-items: center; }
    .filters .unit { min-width: 240px; }
    .filters .search { min-width: 280px; }
    .count { color: var(--mat-sys-on-surface-variant); margin-bottom: 20px; }
    .layout { display: grid; grid-template-columns: 1fr; gap: 16px; align-items: start; }
    .layout.with-record { grid-template-columns: minmax(0, 1.6fr) minmax(320px, 1fr); }
    .scroll { overflow-x: auto; }
    .results { width: 100%; border-collapse: collapse; }
    .results th, .results td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--mat-sys-outline-variant); vertical-align: top; }
    .results th { font: var(--mat-sys-label-large); position: sticky; top: 0; background: var(--mat-sys-surface); }
    .results.compact { margin-bottom: 8px; }
    .student-row { cursor: pointer; }
    .student-row:hover, .student-row:focus { background: var(--mat-sys-surface-container-high); outline: none; }
    .student-row.active { background: var(--mat-sys-secondary-container); }
    .name { font-weight: 500; }
    .nowrap { white-space: nowrap; }
    .level-expert, .level-advanced { color: var(--mat-sys-primary); font-weight: 600; }
    .level-none { color: var(--mat-sys-on-surface-variant); }
    .record { position: sticky; top: 8px; max-height: calc(100vh - 32px); overflow-y: auto; }
    .record .close { margin-left: auto; }
    .record h3 { margin: 12px 0 6px; font: var(--mat-sys-title-small); }
    .class { padding: 8px 0; border-bottom: 1px solid var(--mat-sys-outline-variant); }
    .class-head { display: flex; justify-content: space-between; gap: 8px; }
    .comment { font-style: italic; margin-top: 2px; white-space: pre-wrap; }
    .outcome-pass { color: var(--mat-sys-primary); font-weight: 600; }
    .outcome-fail { color: var(--mat-sys-error); font-weight: 600; }
    .outcome-none { color: var(--mat-sys-on-surface-variant); }
    .status { text-transform: capitalize; }
    .small { font-size: 12px; opacity: 0.8; }
  `],
})
export class StudentProgressComponent implements OnInit, OnDestroy {
  private readonly api = inject(StudentProgressApiService);
  private readonly notify = inject(NotificationService);

  readonly rows = signal<StudentRow[]>([]);
  readonly units = signal<string[]>([]);
  readonly selected = signal<StudentRecord | null>(null);
  readonly loading = signal(false);
  filter: StudentFilter = { unit: '', q: '' };
  private recordSub?: Subscription;

  readonly level = levelText;
  readonly results = resultsText;
  readonly result = resultText;
  readonly attendance = attendanceText;
  readonly attendanceShort = attendanceShort;

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.api.list({ ...this.filter }).subscribe({
      next: r => { this.rows.set(r.students); this.units.set(r.units); this.loading.set(false); },
      error: err => { this.loading.set(false); this.notify.error(apiError(err)); },
    });
  }

  ngOnDestroy(): void {
    this.recordSub?.unsubscribe();
  }

  /** The latest click wins: an earlier record still loading is dropped. */
  open(row: StudentRow): void {
    this.recordSub?.unsubscribe();
    this.recordSub = this.api.record(row.user_id).subscribe({
      next: r => this.selected.set(r),
      error: err => this.notify.error(apiError(err)),
    });
  }
}
