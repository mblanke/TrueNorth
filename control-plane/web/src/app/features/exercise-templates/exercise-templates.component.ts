import { Component, OnInit, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
import { MatTableModule } from '@angular/material/table';
import { MatTooltipModule } from '@angular/material/tooltip';
import { AuthService } from '@core/services/auth.service';
import { ExerciseTemplateRow, ExerciseTemplatesApiService } from '@core/services/exercise-templates-api.service';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';
import { ProvenChipComponent } from '../../shared/components/proven-chip/proven-chip.component';
import { CreateFromTemplateComponent } from './create-from-template.component';

/**
 * Exercise templates (ADR 0016), under Exercises: the reusable part of an exercise, its
 * Proven chip, and "Create exercise from template". Staff only (exercise:create): the MESL
 * and the notes are the exercise-control plan.
 */
@Component({
  selector: 'tn-exercise-templates',
  imports: [
    RouterLink, MatButtonModule, MatCardModule, MatChipsModule, MatIconModule, MatTableModule, MatTooltipModule,
    EmptyStateComponent, ProvenChipComponent, CreateFromTemplateComponent,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">library_books</mat-icon>
          <div>
            <div class="tn-kicker">Exercises</div>
            <h1>Exercise templates</h1>
            <p class="subtitle">Objectives, MESL, scenario and range template, written once and reused.</p>
          </div>
        </div>
        <div class="header-actions">
          <a mat-button routerLink="/exercises"><mat-icon>arrow_back</mat-icon> Exercises</a>
          @if (auth.can('exercise:create')) {
            <a mat-raised-button color="primary" routerLink="/exercises/templates/new">
              <mat-icon>add</mat-icon> New template
            </a>
          }
        </div>
      </div>

      @if (creatingFrom(); as t) {
        <mat-card class="mt-2">
          <mat-card-header><mat-card-title>Create exercise from "{{ t.name }}"</mat-card-title></mat-card-header>
          <mat-card-content>
            <tn-create-from-template [templateId]="t.id" [templateName]="t.name" (cancelled)="creatingFrom.set(null)" />
          </mat-card-content>
        </mat-card>
      }

      <section class="workspace">
        @if (loading()) {
          <div class="tn-skeleton-group" aria-busy="true">
            <div class="tn-skeleton tn-skeleton-row"></div>
            <div class="tn-skeleton tn-skeleton-row"></div>
          </div>
        } @else if (loadError()) {
          <tn-empty-state icon="error_outline" title="Templates could not be loaded" message="Try again.">
            <button mat-stroked-button (click)="load()">Retry</button>
          </tn-empty-state>
        } @else if (rows().length === 0) {
          <tn-empty-state icon="library_books" title="No exercise templates yet"
            message="Write one here, or save an existing exercise as a template from the Exercises page.">
            <a mat-stroked-button routerLink="/exercises/templates/new"><mat-icon>add</mat-icon> New template</a>
          </tn-empty-state>
        } @else {
          <div class="table-wrap">
            <table mat-table [dataSource]="rows()" class="full-width">
              <ng-container matColumnDef="name">
                <th mat-header-cell *matHeaderCellDef>Template</th>
                <td mat-cell *matCellDef="let t">
                  <a [routerLink]="['/exercises/templates', t.id]">{{ t.name }}</a>
                  @if (t.shared) { <mat-chip class="shared">Shared</mat-chip> }
                  <div class="sub">Collective · revision {{ t.revision }}</div>
                </td>
              </ng-container>
              <ng-container matColumnDef="shape">
                <th mat-header-cell *matHeaderCellDef>Contents</th>
                <td mat-cell *matCellDef="let t">
                  {{ t.objectives }} objective(s) · {{ t.mesl_events }} serial(s)
                  @if (t.team_names.length) { <div class="sub">{{ t.team_names.join(', ') }}</div> }
                </td>
              </ng-container>
              <ng-container matColumnDef="proven">
                <th mat-header-cell *matHeaderCellDef>Proven</th>
                <td mat-cell *matCellDef="let t"><tn-proven-chip itemType="exercise_template" [itemId]="t.id" /></td>
              </ng-container>
              <ng-container matColumnDef="actions">
                <th mat-header-cell *matHeaderCellDef>Actions</th>
                <td mat-cell *matCellDef="let t">
                  <a mat-button [routerLink]="['/exercises/templates', t.id]">Open</a>
                  @if (auth.can('exercise:create')) {
                    <button mat-button color="primary" (click)="creatingFrom.set(t)"
                            [attr.aria-label]="'Create exercise from ' + t.name">
                      <mat-icon>play_circle</mat-icon> Create exercise
                    </button>
                  }
                </td>
              </ng-container>
              <tr mat-header-row *matHeaderRowDef="columns"></tr>
              <tr mat-row *matRowDef="let row; columns: columns"></tr>
            </table>
          </div>
        }
      </section>
    </div>
  `,
  styles: [`
    .page-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
    .header-actions { display: flex; gap: 8px; align-items: center; }
    .workspace { margin-top: 16px; padding: 20px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-card); }
    td a:not([mat-button]) { color: var(--text-primary); font-weight: 600; text-decoration: none; }
    td a:not([mat-button]):hover { color: var(--accent); text-decoration: underline; }
    .full-width { width: 100%; }
    .sub { font-size: 12px; color: var(--text-muted); }
    .shared { margin-left: 8px; }
    td { vertical-align: top; padding-top: 12px; padding-bottom: 12px; }
  `],
})
export class ExerciseTemplatesComponent implements OnInit {
  private readonly api = inject(ExerciseTemplatesApiService);
  protected readonly auth = inject(AuthService);

  readonly rows = signal<ExerciseTemplateRow[]>([]);
  readonly loading = signal(true);
  readonly loadError = signal(false);
  readonly creatingFrom = signal<ExerciseTemplateRow | null>(null);
  readonly columns = ['name', 'shape', 'proven', 'actions'];

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.loadError.set(false);
    this.api.list().subscribe({
      next: r => { this.rows.set(r); this.loading.set(false); },
      error: () => { this.loading.set(false); this.loadError.set(true); },
    });
  }
}
