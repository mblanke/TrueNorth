import { Component, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { ApiService } from '@core/services/api.service';
import { ExerciseSummary } from '@core/models';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';

@Component({
  selector: 'tn-exercise-selector',
  standalone: true,
  imports: [RouterLink, MatButtonModule, EmptyStateComponent],
  template: `
    <section class="page-container">
      <header class="page-header">
        <div><h1>Ops Center</h1><p class="subtitle">Select an exercise to open its operational workspace.</p></div>
        <a mat-stroked-button routerLink="/exercises">Manage exercises</a>
      </header>
      @if (loading()) {
        <p role="status">Loading exercises…</p>
      } @else if (error()) {
        <div role="alert"><p>Exercises could not be loaded. No readiness information is available.</p>
          <button mat-stroked-button (click)="load()">Try again</button></div>
      } @else if (!exercises().length) {
        <tn-empty-state title="No exercises on this page" message="Create an exercise or return to the previous page.">
          <a mat-flat-button color="primary" routerLink="/exercises">Manage exercises</a>
        </tn-empty-state>
      } @else {
        <div class="exercise-list">
          @for (exercise of exercises(); track exercise.id) {
            <article>
              <div><h2>{{ exercise.name }}</h2><span class="status-chip" [class]="exercise.state">{{ exercise.state }}</span></div>
              <nav [attr.aria-label]="exercise.name + ' actions'">
                <a mat-stroked-button [routerLink]="['/exercises', exercise.id]">Details</a>
                <a mat-flat-button color="primary" [routerLink]="['/ops-center', exercise.id]">Open Ops Center</a>
              </nav>
            </article>
          }
        </div>
      }
      <nav class="pagination" aria-label="Exercise pages">
        <button mat-button (click)="page(-1)" [disabled]="loading() || offset() === 0">Previous</button>
        <span>Page {{ offset() / pageSize + 1 }}</span>
        <button mat-button (click)="page(1)" [disabled]="loading() || error() || exercises().length < pageSize">Next</button>
      </nav>
    </section>
  `,
  styles: [`
    .exercise-list { display: grid; gap: 12px; }
    article { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
      gap: 16px; padding: 20px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-card); }
    article nav, .pagination { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
    h2 { margin-bottom: 8px; }
    .pagination { justify-content: flex-end; margin-top: 20px; }
  `],
})
export class ExerciseSelectorComponent {
  private readonly api = inject(ApiService);
  readonly pageSize = 25;
  readonly offset = signal(0);
  readonly loading = signal(true);
  readonly error = signal(false);
  readonly exercises = signal<ExerciseSummary[]>([]);

  constructor() { this.load(); }

  load(): void {
    this.loading.set(true);
    this.error.set(false);
    this.api.listExercises(this.pageSize, this.offset()).subscribe({
      next: exercises => { this.exercises.set(exercises); this.loading.set(false); },
      error: () => { this.error.set(true); this.loading.set(false); },
    });
  }

  page(direction: number): void {
    this.offset.update(offset => Math.max(0, offset + direction * this.pageSize));
    this.load();
  }
}
