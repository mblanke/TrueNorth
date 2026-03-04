import { Component, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatStepperModule } from '@angular/material/stepper';
import { MatChipsModule } from '@angular/material/chips';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MatTooltipModule } from '@angular/material/tooltip';

interface TimelineEvent {
  id: string;
  type: string;
  delay_minutes: number;
  description: string;
  technique: string;
}

@Component({
  selector: 'tn-scenario-builder',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatCardModule, MatButtonModule,
    MatIconModule, MatFormFieldModule, MatInputModule, MatSelectModule,
    MatStepperModule, MatChipsModule, MatSnackBarModule, MatTooltipModule,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">construction</mat-icon>
          <div>
            <h1>Scenario Builder</h1>
            <p class="subtitle">Visually construct attack timelines with drag-and-drop events.</p>
          </div>
        </div>
      </div>

      <mat-stepper linear class="tn-stepper">
        <!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• STEP 1: METADATA â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
        <mat-step label="Metadata">
          <div class="step-content">
            <mat-form-field appearance="outline" class="full-width">
              <mat-label>Scenario Name</mat-label>
              <input matInput [(ngModel)]="name" placeholder="APT Campaign Simulation">
            </mat-form-field>

            <div class="form-row">
              <mat-form-field appearance="outline">
                <mat-label>Difficulty</mat-label>
                <mat-select panelClass="tn-select-panel" [(ngModel)]="difficulty">
                  <mat-option value="beginner">Beginner</mat-option>
                  <mat-option value="intermediate">Intermediate</mat-option>
                  <mat-option value="advanced">Advanced</mat-option>
                </mat-select>
              </mat-form-field>
              <mat-form-field appearance="outline">
                <mat-label>Duration (minutes)</mat-label>
                <input matInput type="number" [(ngModel)]="duration">
              </mat-form-field>
            </div>

            <div class="form-row">
              <mat-form-field appearance="outline">
                <mat-label>Description</mat-label>
                <input matInput [(ngModel)]="scenarioDescription" placeholder="Brief scenario overview">
              </mat-form-field>
            </div>

            <div class="step-actions">
              <button mat-raised-button color="primary" matStepperNext>Next</button>
            </div>
          </div>
        </mat-step>

        <!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• STEP 2: TIMELINE â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
        <mat-step label="Timeline">
          <div class="step-content">
            <div class="timeline-toolbar">
              <span class="timeline-count">{{ events().length }} events</span>
              <button mat-raised-button color="primary" (click)="addEvent()">
                <mat-icon>add</mat-icon> Add Event
              </button>
            </div>

            <div class="timeline-editor">
              @for (event of events(); track event.id; let i = $index) {
                <mat-card class="timeline-event">
                  <mat-card-content>
                    <div class="event-row">
                      <span class="event-index">{{ i + 1 }}</span>
                      <mat-form-field appearance="outline" class="event-field">
                        <mat-label>Type</mat-label>
                        <mat-select panelClass="tn-select-panel" [(ngModel)]="event.type">
                          <mat-option value="email_phish">Email Phish</mat-option>
                          <mat-option value="dns_spike">DNS Spike</mat-option>
                          <mat-option value="http_burst">HTTP Burst</mat-option>
                          <mat-option value="simulated_execution">Simulated Execution</mat-option>
                          <mat-option value="identity_new_admin_user">Identity â€” New Admin</mat-option>
                          <mat-option value="lateral_movement">Lateral Movement</mat-option>
                          <mat-option value="data_exfiltration">Data Exfiltration</mat-option>
                        </mat-select>
                      </mat-form-field>
                      <mat-form-field appearance="outline" class="event-field-sm">
                        <mat-label>Delay (min)</mat-label>
                        <input matInput type="number" [(ngModel)]="event.delay_minutes">
                      </mat-form-field>
                      <mat-form-field appearance="outline" class="event-field-sm">
                        <mat-label>MITRE ATT&amp;CK</mat-label>
                        <input matInput [(ngModel)]="event.technique" placeholder="T1566.001">
                      </mat-form-field>
                      <button mat-icon-button color="warn" (click)="removeEvent(i)" matTooltip="Remove event">
                        <mat-icon>delete</mat-icon>
                      </button>
                    </div>
                    <mat-form-field appearance="outline" class="full-width">
                      <mat-label>Description</mat-label>
                      <input matInput [(ngModel)]="event.description" placeholder="What happens in this event">
                    </mat-form-field>
                  </mat-card-content>
                </mat-card>
              }
              <p *ngIf="events().length === 0" class="empty-state">
                No events yet. Click <strong>Add Event</strong> to build your attack timeline.
              </p>
            </div>

            <div class="step-actions">
              <button mat-button matStepperPrevious>Back</button>
              <button mat-raised-button color="primary" matStepperNext [disabled]="events().length === 0">Next</button>
            </div>
          </div>
        </mat-step>

        <!-- â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• STEP 3: PREVIEW & EXPORT â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• -->
        <mat-step label="Preview & Export">
          <div class="step-content">
            <mat-card class="preview-card">
              <mat-card-header>
                <mat-card-title>Generated YAML</mat-card-title>
              </mat-card-header>
              <mat-card-content>
                <pre class="yaml-preview">{{ generateYaml() }}</pre>
              </mat-card-content>
            </mat-card>

            <div class="step-actions">
              <button mat-button matStepperPrevious>Back</button>
              <button mat-raised-button color="primary" (click)="copyYaml()">
                <mat-icon>content_copy</mat-icon> Copy YAML
              </button>
            </div>
          </div>
        </mat-step>
      </mat-stepper>
    </div>
  `,
  styles: [`
    /* â”€â”€ Page header â”€â”€â”€ */
    .page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
    .header-left { display: flex; align-items: center; gap: 16px; }
    h1 { margin: 0; font-size: 24px; color: var(--text-primary); }
    .subtitle { margin: 4px 0 0; color: var(--text-secondary); font-size: 14px; }

    /* â”€â”€ Stepper content â”€â”€â”€ */
    .step-content { padding: 24px 0; }
    .step-actions { display: flex; gap: 12px; margin-top: 16px; }
    .full-width { width: 100%; }

    /* â”€â”€ Form layout â”€â”€â”€ */
    .form-row { display: flex; gap: 16px; margin-bottom: 4px; }
    .form-row mat-form-field { flex: 1; min-width: 0; }

    /* â”€â”€ Timeline â”€â”€â”€ */
    .timeline-toolbar {
      display: flex; justify-content: space-between; align-items: center;
      margin-bottom: 16px;
    }
    .timeline-count { color: var(--text-secondary); font-size: 14px; }
    .timeline-editor { margin-bottom: 8px; }
    .timeline-event {
      margin-bottom: 12px;
      background: var(--bg-card);
      border: 1px solid var(--border);
    }
    .event-row {
      display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
    }
    .event-index {
      font-size: 20px; font-weight: 700; color: var(--accent);
      min-width: 32px; text-align: center;
    }
    .event-field { flex: 1; min-width: 180px; }
    .event-field-sm { min-width: 140px; flex: 0 0 160px; }
    .empty-state {
      text-align: center; padding: 40px; color: var(--text-secondary);
      border: 1px dashed var(--border); border-radius: 8px;
    }

    /* â”€â”€ Preview â”€â”€â”€ */
    .preview-card {
      background: var(--bg-card); border: 1px solid var(--border);
    }
    .yaml-preview {
      background: var(--bg-secondary); color: var(--text-primary);
      padding: 16px; border-radius: 8px;
      font-family: 'Cascadia Code', 'Consolas', monospace; font-size: 13px;
      overflow-x: auto; max-height: 400px; overflow-y: auto;
      border: 1px solid var(--border); margin: 0;
      line-height: 1.6;
    }

    /* â”€â”€ Material overrides in this component â”€â”€â”€ */
    mat-card { background: var(--bg-card); border: 1px solid var(--border); }
    table { background: transparent !important; }
    th, td { color: var(--text-primary) !important; }
  `],
})
export class ScenarioBuilderComponent {
  name = '';
  difficulty = 'intermediate';
  duration = 60;
  scenarioDescription = '';
  events = signal<TimelineEvent[]>([]);

  private counter = 0;

  constructor(private snack: MatSnackBar) {}

  addEvent(): void {
    this.counter++;
    this.events.update(e => [...e, {
      id: `event-${this.counter}`,
      type: 'simulated_execution',
      delay_minutes: this.counter * 5,
      description: '',
      technique: '',
    }]);
  }

  removeEvent(index: number): void {
    this.events.update(e => e.filter((_, i) => i !== index));
  }

  generateYaml(): string {
    const lines: string[] = [
      `id: ${this.name.toLowerCase().replace(/\\s+/g, '-') || 'untitled'}`,
      `name: "${this.name}"`,
      `version: "1.0"`,
      `difficulty: ${this.difficulty}`,
      `duration_minutes: ${this.duration}`,
    ];
    if (this.scenarioDescription) {
      lines.push(`description: "${this.scenarioDescription}"`);
    }
    lines.push(`timeline:`);
    for (const ev of this.events()) {
      lines.push(`  - id: ${ev.id}`);
      lines.push(`    type: ${ev.type}`);
      lines.push(`    delay_minutes: ${ev.delay_minutes}`);
      if (ev.technique) lines.push(`    technique: ${ev.technique}`);
      if (ev.description) lines.push(`    description: "${ev.description}"`);
    }
    return lines.join('\n');
  }

  copyYaml(): void {
    navigator.clipboard.writeText(this.generateYaml()).then(() => {
      this.snack.open('YAML copied to clipboard', 'OK', { duration: 3000 });
    });
  }
}


