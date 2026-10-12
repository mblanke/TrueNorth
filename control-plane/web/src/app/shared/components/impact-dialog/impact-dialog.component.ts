import { Component, inject } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MAT_DIALOG_DATA, MatDialogModule, MatDialogRef } from '@angular/material/dialog';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';

/**
 * A confirmation that states the impact before an operator acts (R0-B). Optional: a
 * required reason (sent to the API and kept in the audit log) and a typed confirmation
 * (the operator repeats an id or name). Closes with `{ reason }` on confirm, or `null`.
 */
export interface ImpactDialogData {
  title: string;
  /** What will happen, one line each. Shown as a list. */
  impact: string[];
  confirmText: string;
  /** Destructive styling on the confirm button. */
  danger?: boolean;
  reasonLabel?: string;
  /** Minimum reason length when a reason is asked for (the API's own floor). */
  reasonMin?: number;
  /** The operator must type this exactly before confirming. */
  typeToConfirm?: string;
}

export interface ImpactDialogResult {
  reason: string;
}

@Component({
  selector: 'tn-impact-dialog',
  imports: [FormsModule, MatDialogModule, MatButtonModule, MatFormFieldModule, MatInputModule],
  template: `
    <h2 mat-dialog-title id="impact-title">{{ data.title }}</h2>
    <mat-dialog-content aria-describedby="impact-list">
      <p class="lead">What this does:</p>
      <ul id="impact-list" class="impact">
        @for (line of data.impact; track $index) {
          <li>{{ line }}</li>
        }
      </ul>
      @if (data.reasonLabel) {
        <mat-form-field appearance="outline" class="full">
          <mat-label>{{ data.reasonLabel }}</mat-label>
          <textarea matInput rows="3" [(ngModel)]="reason" name="reason" cdkFocusInitial
                    [attr.aria-describedby]="'reason-hint'" required></textarea>
          <mat-hint id="reason-hint">At least {{ data.reasonMin || 1 }} characters; kept in the audit log.</mat-hint>
        </mat-form-field>
      }
      @if (data.typeToConfirm) {
        <mat-form-field appearance="outline" class="full">
          <mat-label>Type {{ data.typeToConfirm }} to confirm</mat-label>
          <input matInput [(ngModel)]="typed" name="typed" autocomplete="off" required>
        </mat-form-field>
      }
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-button type="button" [mat-dialog-close]="null">Cancel</button>
      <button mat-raised-button type="button" [color]="data.danger ? 'warn' : 'primary'"
              [disabled]="!ready()" (click)="confirm()">{{ data.confirmText }}</button>
    </mat-dialog-actions>
  `,
  styles: [`
    .lead { margin: 0 0 4px; color: var(--text-secondary); }
    .impact { margin: 0 0 16px; padding-left: 20px; }
    .impact li { margin-bottom: 6px; line-height: 1.4; }
    .full { width: 100%; }
  `],
})
export class ImpactDialogComponent {
  private ref = inject<MatDialogRef<ImpactDialogComponent, ImpactDialogResult | null>>(MatDialogRef);
  data = inject<ImpactDialogData>(MAT_DIALOG_DATA);
  reason = '';
  typed = '';

  ready(): boolean {
    if (this.data.reasonLabel && this.reason.trim().length < (this.data.reasonMin || 1)) return false;
    if (this.data.typeToConfirm && this.typed.trim() !== this.data.typeToConfirm) return false;
    return true;
  }

  confirm(): void {
    if (this.ready()) this.ref.close({ reason: this.reason.trim() });
  }
}
