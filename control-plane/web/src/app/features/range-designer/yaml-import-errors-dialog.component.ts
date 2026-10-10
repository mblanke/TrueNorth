import { Component, inject } from '@angular/core';
import { MAT_DIALOG_DATA, MatDialogModule } from '@angular/material/dialog';
import { MatButtonModule } from '@angular/material/button';

export interface YamlImportErrorsData {
  title: string;
  fileName: string;
  messages: string[];
}

/** Shown instead of loading when a YAML import fails: every reason, not just the first. */
@Component({
  selector: 'tn-yaml-import-errors-dialog',
  imports: [MatDialogModule, MatButtonModule],
  template: `
    <h2 mat-dialog-title>{{ data.title }}</h2>
    <mat-dialog-content>
      <p class="lead">{{ data.fileName }} was not loaded. The canvas is unchanged.</p>
      <ul class="errors" aria-label="Import errors">
        @for (m of shown; track $index) {
          <li>{{ m }}</li>
        }
      </ul>
      @if (hidden > 0) {
        <p class="more">and {{ hidden }} more</p>
      }
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-raised-button color="primary" mat-dialog-close cdkFocusInitial>Close</button>
    </mat-dialog-actions>
  `,
  styles: [`
    .lead { margin: 0 0 8px; }
    .errors { margin: 0; padding-left: 20px; max-height: 320px; overflow: auto; }
    .errors li { font-family: var(--font-mono, monospace); font-size: 12px; white-space: pre-wrap; margin-bottom: 4px; }
    .more { color: var(--text-muted); font-size: 12px; }
  `],
})
export class YamlImportErrorsDialogComponent {
  static readonly MAX_SHOWN = 20;
  data = inject<YamlImportErrorsData>(MAT_DIALOG_DATA);
  shown = this.data.messages.slice(0, YamlImportErrorsDialogComponent.MAX_SHOWN);
  hidden = Math.max(0, this.data.messages.length - YamlImportErrorsDialogComponent.MAX_SHOWN);
}
