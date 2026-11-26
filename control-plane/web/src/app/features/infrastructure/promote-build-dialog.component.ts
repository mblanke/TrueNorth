// Author: Marc-Andre Blanke
// Created: 2026-11-23T16:00:00Z
import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MAT_DIALOG_DATA, MatDialogModule, MatDialogRef } from '@angular/material/dialog';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';

export const PROMOTE_REASON_MAX = 500;

export interface PromoteBuildDialogData {
  imageName: string;
  buildId: string;
  /** What promoting changes, one sentence per line. */
  lines: string[];
}

/**
 * Asks the platform administrator to confirm a promotion and say why (ADR 0013 §2).
 * Closes with the trimmed reason, or with nothing when cancelled. The reason is kept in
 * the audit entry, so it is required.
 */
@Component({
  selector: 'tn-promote-build-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, MatDialogModule, MatButtonModule, MatFormFieldModule, MatInputModule],
  template: `
    <h2 mat-dialog-title>Promote build {{ data.buildId }}?</h2>
    <mat-dialog-content>
      @for (line of data.lines; track $index) {
        <p class="line">{{ line }}</p>
      }
      <mat-form-field appearance="outline" class="reason">
        <mat-label>Reason (kept in the audit log)</mat-label>
        <textarea matInput data-testid="gi-reason" rows="3" required cdkFocusInitial
          [maxlength]="max" [(ngModel)]="reason"></textarea>
        <mat-hint align="end">{{ reason.length }} / {{ max }}</mat-hint>
        @if (touched && !valid()) {
          <mat-error>Say why this build is being promoted.</mat-error>
        }
      </mat-form-field>
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-button type="button" data-testid="gi-promote-cancel" (click)="cancel()">Cancel</button>
      <button mat-flat-button color="primary" type="button" data-testid="gi-promote-confirm"
        [disabled]="!valid()" (click)="confirm()">Promote</button>
    </mat-dialog-actions>
  `,
  styles: [`
    .line { margin: 0 0 8px; }
    .reason { width: 100%; margin-top: 8px; }
  `],
})
export class PromoteBuildDialogComponent {
  private ref = inject<MatDialogRef<PromoteBuildDialogComponent, string>>(MatDialogRef);
  readonly data = inject<PromoteBuildDialogData>(MAT_DIALOG_DATA);
  readonly max = PROMOTE_REASON_MAX;
  reason = '';
  touched = false;

  valid(): boolean {
    const r = this.reason.trim();
    return r.length > 0 && r.length <= this.max;
  }

  confirm(): void {
    this.touched = true;
    if (this.valid()) this.ref.close(this.reason.trim());
  }

  cancel(): void {
    this.ref.close();
  }
}
