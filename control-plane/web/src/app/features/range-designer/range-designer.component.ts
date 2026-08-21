import {
  Component,
  OnDestroy,
  ElementRef,
  ViewChild,
  AfterViewInit,
  HostListener,
  Inject,
  signal,
  ChangeDetectorRef,
  ViewEncapsulation,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatSliderModule } from '@angular/material/slider';
import { MatDividerModule } from '@angular/material/divider';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import {
  MatDialog,
  MatDialogModule,
  MatDialogRef,
  MAT_DIALOG_DATA,
} from '@angular/material/dialog';
import { ActivatedRoute, Router, RouterModule } from '@angular/router';
import { Observable } from 'rxjs';
import * as joint from 'jointjs';
import { FilterCategoryPipe } from './filter-category.pipe';
import { GraphHistory } from './graph-history';
import { ApiService } from '@core/services/api.service';
import { RangeNotesComponent } from '../../shared/components/range-notes/range-notes.component';
import { Range, Template } from '@core/models';
import { ConfirmDialogComponent } from '../../shared/components/confirm-dialog/confirm-dialog.component';
import { EmptyStateComponent } from '../../shared/components/empty-state/empty-state.component';

/* ----------------------------------------------------------------
   Stencil types for the palette
   ---------------------------------------------------------------- */
interface StencilItem {
  type: string;
  label: string;
  icon: string;
  category: 'compute' | 'network' | 'security' | 'zone';
  defaults: Record<string, string>;
}

/** One entry in the OS Template picker. */
interface OsOption {
  value: string;
  label: string;
}

/**
 * Shown when the control plane has no golden-image catalogue to offer (fresh
 * install, hypervisor unreachable, endpoint not deployed yet). These are the
 * aliases the seeded templates use, so a designer opened against an empty
 * catalogue still produces YAML the provisioner understands.
 */
const FALLBACK_OS_OPTIONS: OsOption[] = [
  { value: 'ubuntu-22.04', label: 'Ubuntu 22.04' },
  { value: 'ubuntu-24.04', label: 'Ubuntu 24.04' },
  { value: 'windows-server-2022', label: 'Windows Server 2022' },
  { value: 'windows-11', label: 'Windows 11' },
  { value: 'kali-2024', label: 'Kali Linux 2024' },
  { value: 'rocky-9', label: 'Rocky Linux 9' },
  { value: 'security-onion', label: 'Security Onion' },
  { value: 'pfsense', label: 'pfSense' },
  { value: 'vyos', label: 'VyOS' },
];

/* ----------------------------------------------------------------
   Custom JointJS shapes
   ---------------------------------------------------------------- */
const GRID = 20;
const NODE_W = 120;
const NODE_H = 80;

function createNodeShape(
  type: string,
  label: string,
  iconChar: string,
  color: string,
  x: number,
  y: number,
  extra: Record<string, string> = {},
): joint.dia.Element {
  const el = new joint.shapes.standard.Rectangle({
    position: { x, y },
    size: { width: NODE_W, height: NODE_H },
    attrs: {
      body: {
        fill: 'var(--bg-card)',
        stroke: color,
        strokeWidth: 2,
        rx: 8,
        ry: 8,
        filter: 'none',
      },
      label: {
        text: label,
        fill: 'var(--text-primary)',
        fontSize: 12,
        fontFamily: 'Inter, Segoe UI, sans-serif',
        textAnchor: 'middle',
        textVerticalAnchor: 'top',
        refX: '50%',
        refY: '65%',
      },
    },
    ports: {
      groups: {
        in: {
          position: 'left',
          attrs: {
            circle: { fill: color, stroke: 'var(--border)', strokeWidth: 1, r: 5, magnet: true },
          },
          label: { position: 'outside' },
        },
        out: {
          position: 'right',
          attrs: {
            circle: { fill: color, stroke: 'var(--border)', strokeWidth: 1, r: 5, magnet: true },
          },
          label: { position: 'outside' },
        },
      },
      items: [
        { group: 'in', id: 'in1' },
        { group: 'out', id: 'out1' },
      ],
    },
  });
  el.prop('nodeType', type);
  el.prop('nodeData', { label, ...extra });
  return el;
}

function createSubnetZone(
  label: string, x: number, y: number, w: number, h: number, color: string,
): joint.dia.Element {
  const zone = new joint.shapes.standard.Rectangle({
    position: { x, y },
    size: { width: w, height: h },
    attrs: {
      body: {
        fill: color + '15',
        stroke: color,
        strokeWidth: 2,
        strokeDasharray: '8 4',
        rx: 12,
        ry: 12,
      },
      label: {
        text: label,
        fill: color,
        fontSize: 14,
        fontFamily: 'Inter, Segoe UI, sans-serif',
        fontWeight: 'bold',
        textAnchor: 'start',
        textVerticalAnchor: 'top',
        refX: 12,
        refY: 8,
      },
    },
  });
  zone.prop('nodeType', 'subnet');
  zone.prop('nodeData', { label, cidr: '10.0.0.0/24' });
  return zone;
}

/* ----------------------------------------------------------------
   Text prompt dialog — replaces window.prompt()
   ---------------------------------------------------------------- */
export interface TextPromptData {
  title: string;
  label: string;
  value: string;
  confirmText?: string;
}

/**
 * A one-field dialog. The native prompt() it replaces blocked the whole tab,
 * could not be themed, and is silently suppressed by some browsers when a page
 * is not the active tab — which made renaming a node look like a dead click.
 */
@Component({
  selector: 'tn-text-prompt-dialog',
  standalone: true,
  imports: [FormsModule, MatDialogModule, MatButtonModule, MatFormFieldModule, MatInputModule],
  template: `
    <h2 mat-dialog-title>{{ data.title }}</h2>
    <mat-dialog-content>
      <mat-form-field appearance="outline" class="prompt-field">
        <mat-label>{{ data.label }}</mat-label>
        <input matInput [(ngModel)]="value" (keyup.enter)="submit()" cdkFocusInitial>
      </mat-form-field>
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-button (click)="dialogRef.close()">Cancel</button>
      <button mat-raised-button color="primary" [disabled]="!value.trim()" (click)="submit()">
        {{ data.confirmText || 'OK' }}
      </button>
    </mat-dialog-actions>
  `,
  styles: [`
    .prompt-field { width: 100%; min-width: 320px; }
  `],
})
export class TextPromptDialogComponent {
  value: string;

  constructor(
    public dialogRef: MatDialogRef<TextPromptDialogComponent, string | undefined>,
    @Inject(MAT_DIALOG_DATA) public data: TextPromptData,
  ) {
    this.value = data.value ?? '';
  }

  submit(): void {
    const trimmed = this.value.trim();
    if (!trimmed) return;
    this.dialogRef.close(trimmed);
  }
}

@Component({
  selector: 'tn-range-designer',
  standalone: true,
  encapsulation: ViewEncapsulation.None,
  imports: [
    CommonModule, FormsModule, RouterModule, MatCardModule, MatButtonModule, MatIconModule,
    MatFormFieldModule, MatInputModule, MatSelectModule, MatCheckboxModule, MatTooltipModule,
    MatSliderModule, MatDividerModule, MatSnackBarModule, MatDialogModule, FilterCategoryPipe,
    RangeNotesComponent,
    EmptyStateComponent,
  ],
  template: `
    <div class="designer-layout">
      <!-- LEFT: STENCIL PALETTE -->
      <aside class="palette">
        <h3 class="palette-title">Components</h3>

        <div class="palette-section">
          <div class="section-label">Compute</div>
          @for (item of stencils | filterCategory:'compute'; track item.type) {
            <div class="stencil-item" draggable="true"
                 (dragstart)="onDragStart($event, item)"
                 [matTooltip]="item.label">
              <mat-icon class="stencil-icon">{{ item.icon }}</mat-icon>
              <span class="stencil-label">{{ item.label }}</span>
            </div>
          }
        </div>

        <mat-divider></mat-divider>

        <div class="palette-section">
          <div class="section-label">Network</div>
          @for (item of stencils | filterCategory:'network'; track item.type) {
            <div class="stencil-item" draggable="true"
                 (dragstart)="onDragStart($event, item)"
                 [matTooltip]="item.label">
              <mat-icon class="stencil-icon">{{ item.icon }}</mat-icon>
              <span class="stencil-label">{{ item.label }}</span>
            </div>
          }
        </div>

        <mat-divider></mat-divider>

        <div class="palette-section">
          <div class="section-label">Security</div>
          @for (item of stencils | filterCategory:'security'; track item.type) {
            <div class="stencil-item" draggable="true"
                 (dragstart)="onDragStart($event, item)"
                 [matTooltip]="item.label">
              <mat-icon class="stencil-icon">{{ item.icon }}</mat-icon>
              <span class="stencil-label">{{ item.label }}</span>
            </div>
          }
        </div>

        <mat-divider></mat-divider>

        <div class="palette-section">
          <div class="section-label">Zones</div>
          @for (item of stencils | filterCategory:'zone'; track item.type) {
            <div class="stencil-item" draggable="true"
                 (dragstart)="onDragStart($event, item)"
                 [matTooltip]="item.label">
              <mat-icon class="stencil-icon">{{ item.icon }}</mat-icon>
              <span class="stencil-label">{{ item.label }}</span>
            </div>
          }
        </div>

        <mat-divider></mat-divider>

        <!-- Canvas controls -->
        <div class="palette-section">
          <div class="section-label">Canvas</div>
          <div class="canvas-controls">
            <button mat-icon-button matTooltip="Zoom In" (click)="zoomIn()"><mat-icon>zoom_in</mat-icon></button>
            <button mat-icon-button matTooltip="Zoom Out" (click)="zoomOut()"><mat-icon>zoom_out</mat-icon></button>
            <button mat-icon-button matTooltip="Fit to Screen" (click)="fitContent()"><mat-icon>fit_screen</mat-icon></button>
            <button mat-icon-button matTooltip="Clear Canvas" (click)="clearCanvas()"><mat-icon>delete_sweep</mat-icon></button>
          </div>
        </div>
      </aside>

      <!-- CENTER: JOINTJS CANVAS -->
      <section class="canvas-area"
               (dragover)="onDragOver($event)"
               (drop)="onDrop($event)">
        <div class="canvas-toolbar">
          <a mat-icon-button routerLink="/authoring/ranges" matTooltip="Back to ranges">
            <mat-icon>arrow_back</mat-icon>
          </a>
          <span class="canvas-title">Range Topology</span>
          <span class="canvas-info">{{ elementCount() }} nodes &middot; {{ linkCount() }} links</span>
          <span class="spacer"></span>
          <button mat-icon-button matTooltip="Undo (Ctrl+Z)" (click)="undo()" [disabled]="!canUndo()">
            <mat-icon>undo</mat-icon>
          </button>
          <button mat-icon-button matTooltip="Redo (Ctrl+Y)" (click)="redo()" [disabled]="!canRedo()">
            <mat-icon>redo</mat-icon>
          </button>
          <button mat-stroked-button (click)="autoLayout()">
            <mat-icon>auto_fix_high</mat-icon> Auto Layout
          </button>
          <button mat-stroked-button color="primary" (click)="saveDiagram()" [disabled]="!rangeId()"
                  [matTooltip]="dirty() ? 'Unsaved changes' : 'Save diagram'">
            <mat-icon>save</mat-icon> Save
            @if (dirty()) {
              <span class="dirty-dot"></span>
            }
          </button>
          <button mat-stroked-button [routerLink]="['/topology-3d']" [queryParams]="{ range: rangeId() }"
                  [disabled]="!rangeId()" matTooltip="View the saved diagram in 3D">
            <mat-icon>3d_rotation</mat-icon> View in 3D
          </button>
          <button mat-stroked-button (click)="exportYaml()">
            <mat-icon>download</mat-icon> Export YAML
          </button>
          <button mat-stroked-button (click)="importYaml()">
            <mat-icon>upload</mat-icon> Import YAML
          </button>
          <button mat-stroked-button (click)="importNmap()" class="nmap-btn">
            <mat-icon>radar</mat-icon> Import Nmap
          </button>
        </div>
        <div class="shortcut-hint">
          Ctrl+S save &middot; Ctrl+Z undo &middot; Ctrl+Y redo &middot; Ctrl+D duplicate &middot;
          Del remove &middot; double-click a node to rename
        </div>
        <div class="canvas-stage">
          <div #canvas class="joint-canvas"></div>

          @if (showPicker()) {
            <div class="picker-overlay">
              <div class="picker-card">
                <tn-empty-state icon="rocket_launch"
                                title="Start a range design"
                                message="Open a saved range, start from a template, or draw on a blank canvas.">
                  <div class="picker-actions">
                    <div class="picker-row">
                      <mat-form-field appearance="outline" subscriptSizing="dynamic" class="picker-field">
                        <mat-label>Open a range</mat-label>
                        <mat-select [(ngModel)]="pickerRangeId">
                          @for (r of ranges(); track r.id) {
                            <mat-option [value]="r.id">{{ r.name }}</mat-option>
                          }
                        </mat-select>
                      </mat-form-field>
                      <button mat-stroked-button color="primary"
                              [disabled]="!pickerRangeId" (click)="openSelectedRange()">
                        Open
                      </button>
                    </div>

                    <div class="picker-row">
                      <mat-form-field appearance="outline" subscriptSizing="dynamic" class="picker-field">
                        <mat-label>New from template</mat-label>
                        <mat-select [(ngModel)]="pickerTemplateId">
                          @for (t of templates(); track t.id) {
                            <mat-option [value]="t.id">{{ t.name }}</mat-option>
                          }
                        </mat-select>
                      </mat-form-field>
                      <button mat-stroked-button color="primary"
                              [disabled]="!pickerTemplateId" (click)="startFromSelectedTemplate()">
                        Create
                      </button>
                    </div>

                    <button mat-button class="blank-btn" (click)="dismissPicker()">
                      <mat-icon>draw</mat-icon> Blank canvas
                    </button>
                  </div>
                </tn-empty-state>
              </div>
            </div>
          }
        </div>
      </section>

      <!-- RIGHT: PROPERTIES PANEL -->
      <aside class="properties">
        <h3 class="props-title">Properties</h3>
        @if (selectedNode()) {
          <div class="props-form">
            <mat-form-field appearance="outline" subscriptSizing="dynamic" class="full-width">
              <mat-label>Label</mat-label>
              <input matInput [(ngModel)]="propLabel" (ngModelChange)="updateNodeProp('label', $event)">
            </mat-form-field>

            <mat-form-field appearance="outline" subscriptSizing="dynamic" class="full-width">
              <mat-label>Hostname</mat-label>
              <input matInput [(ngModel)]="propHostname" (ngModelChange)="updateNodeData('hostname', $event)">
            </mat-form-field>

            <mat-form-field appearance="outline" subscriptSizing="dynamic" class="full-width">
              <mat-label>IP Address</mat-label>
              <input matInput [(ngModel)]="propIp" placeholder="10.0.1.10" (ngModelChange)="updateNodeData('ip', $event)">
            </mat-form-field>

            @if (selectedNodeType() !== 'subnet' && selectedNodeType() !== 'dmz' && selectedNodeType() !== 'cloud') {
              <mat-form-field appearance="outline" subscriptSizing="dynamic" class="full-width">
                <mat-label>OS Template</mat-label>
                <select matNativeControl [(ngModel)]="propOs" (ngModelChange)="onOsChange($event)">
                  <option value="" disabled>-- Select OS --</option>
                  @for (opt of osOptions(); track opt.value) {
                    <option [value]="opt.value">{{ opt.label }}</option>
                  }
                </select>
              </mat-form-field>

              @if (propOs === 'windows-server-2022') {
                <div class="services-section">
                  <div class="section-label-sm">Windows Server Roles &amp; Services</div>
                  <div class="services-grid">
                    @for (svc of windowsServerServices; track svc.id) {
                      <mat-checkbox [checked]="isServiceSelected(svc.id)"
                                    (change)="toggleService(svc.id)"
                                    color="primary"
                                    class="service-cb">
                        <span class="svc-label">{{ svc.label }}</span>
                      </mat-checkbox>
                    }
                  </div>
                </div>
              }

              <div class="prop-row">
                <mat-form-field appearance="outline" subscriptSizing="dynamic">
                  <mat-label>vCPUs</mat-label>
                  <input matInput type="number" [(ngModel)]="propCpu" (ngModelChange)="updateNodeData('vcpu', $event)">
                </mat-form-field>
                <mat-form-field appearance="outline" subscriptSizing="dynamic">
                  <mat-label>RAM (MB)</mat-label>
                  <input matInput type="number" [(ngModel)]="propRam" (ngModelChange)="updateNodeData('ram_mb', $event)">
                </mat-form-field>
              </div>

              <mat-form-field appearance="outline" subscriptSizing="dynamic" class="full-width">
                <mat-label>Disk (GB)</mat-label>
                <input matInput type="number" [(ngModel)]="propDisk" (ngModelChange)="updateNodeData('disk_gb', $event)">
              </mat-form-field>

              <mat-form-field appearance="outline" subscriptSizing="dynamic" class="full-width">
                <mat-label>VLAN ID</mat-label>
                <input matInput type="number" [(ngModel)]="propVlan" (ngModelChange)="updateNodeData('vlan', $event)">
              </mat-form-field>
            }

            @if (selectedNodeType() === 'subnet' || selectedNodeType() === 'dmz') {
              <mat-form-field appearance="outline" subscriptSizing="dynamic" class="full-width">
                <mat-label>CIDR</mat-label>
                <input matInput [(ngModel)]="propCidr" placeholder="10.0.0.0/24" (ngModelChange)="updateNodeData('cidr', $event)">
              </mat-form-field>
            }

            <div class="prop-actions">
              <button mat-stroked-button color="warn" (click)="deleteSelected()">
                <mat-icon>delete</mat-icon> Delete
              </button>
              <button mat-stroked-button (click)="duplicateSelected()">
                <mat-icon>content_copy</mat-icon> Duplicate
              </button>
            </div>
          </div>
        } @else {
          <div class="props-empty">
            <mat-icon class="empty-icon">touch_app</mat-icon>
            <p>Select a node on the canvas to edit its properties.</p>
            <p class="hint">Drag components from the palette to add them.</p>
          </div>
        }

        @if (rangeId(); as rid) {
          <div class="props-notes">
            <tn-range-notes
              [rangeId]="rid"
              [description]="rangeDescription()"
              (descriptionChange)="rangeDescription.set($event)"
            />
          </div>
        }
      </aside>
    </div>

    <!-- Hidden textarea for YAML import -->
    <textarea #yamlInput style="position:absolute;left:-9999px;"></textarea>
  `,
  styles: [`
    :host { display: block; height: calc(100vh - 64px); overflow: hidden; }

    .props-notes {
      margin-top: 14px;
      padding-top: 14px;
      border-top: 1px solid var(--border);
    }

    .designer-layout {
      display: grid;
      grid-template-columns: 220px 1fr 300px;
      min-height: 0;
      height: 100%;
      background: var(--bg-primary);
    }

    /* -- Palette -- */
    .palette {
      background: var(--bg-secondary);
      border-right: 1px solid var(--border);
      overflow-y: auto;
      padding: 12px;
    }
    .palette-title {
      color: var(--text-primary);
      font-size: 16px;
      font-weight: 600;
      margin: 0 0 12px 0;
    }
    .palette-section { margin: 8px 0; }
    .section-label {
      color: var(--text-muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 1px;
      margin-bottom: 6px;
    }
    .stencil-item {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 10px;
      border-radius: 6px;
      cursor: grab;
      border: 1px solid transparent;
      transition: all 0.15s;
      color: var(--text-secondary);
    }
    .stencil-item:hover {
      background: var(--accent-muted);
      border-color: var(--accent);
      color: var(--text-primary);
    }
    .stencil-item:active { cursor: grabbing; }
    .stencil-icon { font-size: 20px; width: 20px; height: 20px; color: var(--accent); }
    .stencil-label { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .canvas-controls { display: flex; gap: 4px; flex-wrap: wrap; }
    .canvas-controls button { color: var(--text-secondary) !important; }

    /* -- Canvas -- */
    .canvas-area {
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .canvas-toolbar {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 8px 16px;
      background: var(--bg-secondary);
      border-bottom: 1px solid var(--border);
    }
    .canvas-title { color: var(--text-primary); font-weight: 600; font-size: 15px; }
    .canvas-info { color: var(--text-muted); font-size: 12px; }
    .canvas-toolbar .spacer { flex: 1; }
    .canvas-toolbar button {
      color: var(--accent) !important;
      border-color: var(--border) !important;
      font-size: 13px;
    }
    .canvas-stage {
      position: relative;
      flex: 1;
      min-height: 0;
      display: flex;
    }
    .joint-canvas {
      flex: 1;
      background:
        linear-gradient(var(--border) 1px, transparent 1px),
        linear-gradient(90deg, var(--border) 1px, transparent 1px);
      background-size: 20px 20px;
      background-color: var(--bg-primary);
      cursor: crosshair;
    }

    /* Unsaved-changes marker on the Save button */
    .dirty-dot {
      display: inline-block;
      width: 8px;
      height: 8px;
      margin-left: 6px;
      border-radius: 50%;
      background: var(--warning);
      vertical-align: middle;
    }

    .shortcut-hint {
      padding: 4px 16px 6px;
      font-size: 11px;
      letter-spacing: 0.2px;
      color: var(--text-muted);
      background: var(--bg-secondary);
      border-bottom: 1px solid var(--border);
    }

    /* Range / template picker shown when the designer opens with no range */
    .picker-overlay {
      position: absolute;
      inset: 0;
      z-index: 5;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
      background: color-mix(in srgb, var(--bg-primary) 86%, transparent);
      backdrop-filter: blur(2px);
    }
    .picker-card {
      width: 100%;
      max-width: 520px;
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 12px;
      box-shadow: 0 18px 48px color-mix(in srgb, var(--bg-primary) 70%, transparent);
    }
    .picker-actions {
      display: flex;
      flex-direction: column;
      gap: 12px;
      width: 100%;
      margin-top: 8px;
    }
    .picker-row {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .picker-field { flex: 1; }
    .blank-btn {
      align-self: center;
      color: var(--text-secondary) !important;
    }
    :host ::ng-deep .picker-card .mdc-floating-label {
      color: var(--text-secondary) !important;
    }
    :host ::ng-deep .picker-card .mat-mdc-select-value-text {
      color: var(--text-primary) !important;
    }
    :host ::ng-deep .picker-card .mdc-notched-outline__leading,
    :host ::ng-deep .picker-card .mdc-notched-outline__notch,
    :host ::ng-deep .picker-card .mdc-notched-outline__trailing {
      border-color: var(--border-light) !important;
    }

    /* -- Properties Panel -- */
    .properties {
      background: var(--bg-secondary);
      border-left: 1px solid var(--border);
      overflow-y: auto;
      padding: 16px;
    }
    .props-title {
      color: var(--text-primary);
      font-size: 16px;
      font-weight: 600;
      margin: 0 0 12px 0;
    }
    .props-form {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .full-width { width: 100%; }

    /* Side-by-side fields */
    .prop-row {
      display: grid;
      grid-template-columns: 1fr;
      gap: 8px;
    }
    .prop-row mat-form-field { width: 100%; }

    /* Compact outline fields in the panel */
    :host ::ng-deep .properties .mat-mdc-form-field {
      --mat-form-field-container-height: 48px;
    }
    :host ::ng-deep .properties .mat-mdc-text-field-wrapper {
      padding: 0 12px !important;
    }
    :host ::ng-deep .properties .mat-mdc-form-field-infix {
      padding-top: 14px !important;
      padding-bottom: 6px !important;
      min-height: 44px !important;
    }
    :host ::ng-deep .properties .mdc-text-field--outlined {
      background: transparent !important;
    }
    :host ::ng-deep .properties .mdc-text-field {
      background: transparent !important;
    }
    :host ::ng-deep .properties .mat-mdc-form-field-subscript-wrapper {
      min-height: 0 !important;
      height: 0 !important;
    }
    :host ::ng-deep .properties .mdc-floating-label {
      color: var(--text-secondary) !important;
      font-size: 13px !important;
    }
    :host ::ng-deep .properties input.mat-mdc-input-element {
      color: var(--text-primary) !important;
      font-size: 13px !important;
    }
    :host ::ng-deep .properties .mat-mdc-select-value-text {
      color: var(--text-primary) !important;
      font-size: 13px !important;
    }
    :host ::ng-deep .properties .mat-mdc-select-arrow {
      color: var(--text-secondary) !important;
    }
    :host ::ng-deep .properties .mdc-notched-outline__leading,
    :host ::ng-deep .properties .mdc-notched-outline__notch,
    :host ::ng-deep .properties .mdc-notched-outline__trailing {
      border-color: var(--border-light) !important;
    }

    /* Nmap import button accent */
    .nmap-btn {
      color: var(--success) !important;
      border-color: var(--success) !important;
    }

    /* Services section */
    .services-section {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
    }
    .section-label-sm {
      color: var(--text-muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      margin-bottom: 8px;
      font-weight: 600;
    }
    .services-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 4px 8px;
    }
    .service-cb {
      display: flex;
      align-items: center;
    }
    .svc-label {
      font-size: 12px;
      color: var(--text-secondary);
    }
    :host ::ng-deep .service-cb .mdc-checkbox {
      --mdc-checkbox-selected-checkmark-color: var(--text-on-accent);
      --mdc-checkbox-selected-icon-color: var(--accent);
      --mdc-checkbox-selected-hover-icon-color: var(--accent-hover);
      --mdc-checkbox-unselected-icon-color: var(--border-light);
      --mdc-checkbox-unselected-hover-icon-color: var(--text-muted);
    }
    :host ::ng-deep .service-cb .mdc-checkbox__background {
      border-radius: 3px;
    }
    :host ::ng-deep .service-cb .mdc-label,
    :host ::ng-deep .service-cb label {
      color: var(--text-secondary) !important;
      font-size: 12px !important;
    }

    /* Native select styling */
    :host ::ng-deep .properties select[matNativeControl] {
      color: var(--text-primary) !important;
      font-size: 13px !important;
      font-family: var(--font-body) !important;
      background: transparent !important;
      cursor: pointer;
    }
    :host ::ng-deep .properties select[matNativeControl] option {
      background: var(--bg-surface);
      color: var(--text-primary);
      font-size: 14px;
      padding: 8px;
    }

    .prop-actions {
      display: flex;
      gap: 8px;
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px solid var(--border);
    }
    .prop-actions button {
      flex: 1;
      font-size: 12px;
    }
    .props-empty {
      text-align: center;
      padding: 40px 16px;
      color: var(--text-muted);
    }
    .empty-icon {
      font-size: 48px;
      width: 48px;
      height: 48px;
      color: var(--border-light);
      margin-bottom: 12px;
    }
    .hint { font-size: 12px; color: var(--text-muted); }
  `],
})
export class RangeDesignerComponent implements AfterViewInit, OnDestroy {
  @ViewChild('canvas', { static: true }) canvasEl!: ElementRef<HTMLDivElement>;
  @ViewChild('yamlInput', { static: true }) yamlInputEl!: ElementRef<HTMLTextAreaElement>;

  private graph!: joint.dia.Graph;
  private paper!: joint.dia.Paper;
  private draggedStencil: StencilItem | null = null;

  /* Signals for template binding */
  elementCount = signal(0);
  linkCount = signal(0);
  selectedNode = signal<joint.dia.Element | null>(null);
  selectedNodeType = signal('');
  rangeId = signal<string | null>(null);
  /** The open range's description, shown and edited in the properties rail. */
  rangeDescription = signal('');
  canUndo = signal(false);
  canRedo = signal(false);
  /** True whenever the canvas holds edits that are not on the server yet. */
  dirty = signal(false);

  /* OS picker — replaced at runtime by the golden-image catalogue if present */
  osOptions = signal<OsOption[]>(FALLBACK_OS_OPTIONS);

  /* Range / template picker overlay */
  showPicker = signal(false);
  ranges = signal<Range[]>([]);
  templates = signal<Template[]>([]);
  pickerRangeId = '';
  pickerTemplateId = '';

  /* Undo/redo. Snapshots are graph.toJSON() payloads. */
  private history = new GraphHistory(50);
  /** Set while a snapshot is being applied, so restoring does not re-record. */
  private suppressHistory = false;
  private snapshotTimer: ReturnType<typeof setTimeout> | null = null;

  /* Property panel bindings */
  propLabel = '';
  propHostname = '';
  propIp = '';
  propOs = '';
  propCpu = 2;
  propRam = 2048;
  propDisk = 40;
  propVlan = 100;
  propCidr = '';
  propServices: string[] = [];

  /* Stencil palette definition */
  stencils: StencilItem[] = [
    { type: 'workstation', label: 'Workstation', icon: 'computer', category: 'compute',
      defaults: { os_template: 'windows-11', vcpu: '2', ram_mb: '4096', disk_gb: '60' } },
    { type: 'server', label: 'Server', icon: 'dns', category: 'compute',
      defaults: { os_template: 'ubuntu-22.04', vcpu: '4', ram_mb: '8192', disk_gb: '100' } },
    { type: 'dc', label: 'Domain Controller', icon: 'domain', category: 'compute',
      defaults: { os_template: 'windows-server-2022', vcpu: '4', ram_mb: '8192', disk_gb: '120' } },
    { type: 'kali', label: 'Kali Attacker', icon: 'bug_report', category: 'compute',
      defaults: { os_template: 'kali-2024', vcpu: '2', ram_mb: '4096', disk_gb: '80' } },
    { type: 'switch', label: 'Switch', icon: 'device_hub', category: 'network',
      defaults: {} },
    { type: 'router', label: 'Router', icon: 'router', category: 'network',
      defaults: { os_template: 'vyos' } },
    { type: 'cloud', label: 'Internet/Cloud', icon: 'cloud', category: 'network',
      defaults: {} },
    { type: 'firewall', label: 'Firewall', icon: 'local_fire_department', category: 'security',
      defaults: { os_template: 'pfsense', vcpu: '2', ram_mb: '2048', disk_gb: '20' } },
    { type: 'seconion', label: 'Security Onion', icon: 'shield', category: 'security',
      defaults: { os_template: 'security-onion', vcpu: '4', ram_mb: '16384', disk_gb: '200' } },
    { type: 'subnet', label: 'Subnet', icon: 'grid_view', category: 'zone',
      defaults: { cidr: '10.0.0.0/24' } },
    { type: 'dmz', label: 'DMZ', icon: 'security', category: 'zone',
      defaults: { cidr: '172.16.0.0/24' } },
  ];

  private nodeColors: Record<string, string> = {
    workstation: '#42A5F5', server: '#66BB6A', dc: '#AB47BC', kali: '#EF5350',
    switch: '#FFA726', router: '#26C6DA', cloud: '#78909C',
    firewall: '#FF7043', seconion: '#5C6BC0', subnet: '#29B6F6', dmz: '#FFCA28',
  };

  /* Windows Server role/service options */
  windowsServerServices = [
    { id: 'ad-ds', label: 'Active Directory' },
    { id: 'ad-cs', label: 'AD Certificate Svcs' },
    { id: 'ad-fs', label: 'AD Federation Svcs' },
    { id: 'dns', label: 'DNS Server' },
    { id: 'dhcp', label: 'DHCP Server' },
    { id: 'iis', label: 'IIS Web Server' },
    { id: 'sql-server', label: 'SQL Server' },
    { id: 'exchange', label: 'Exchange Server' },
    { id: 'sharepoint', label: 'SharePoint' },
    { id: 'file-print', label: 'File & Print' },
    { id: 'wsus', label: 'WSUS' },
    { id: 'hyper-v', label: 'Hyper-V' },
    { id: 'rds', label: 'Remote Desktop' },
    { id: 'ca', label: 'Certificate Authority' },
  ];

  constructor(
    private cdr: ChangeDetectorRef,
    private snack: MatSnackBar,
    private route: ActivatedRoute,
    private router: Router,
    private api: ApiService,
    private dialog: MatDialog,
  ) {}

  ngAfterViewInit(): void {
    this.initGraph();
    this.initPaper();
    this.bindEvents();
    this.loadOsOptions();
    this.resetHistory();

    const id = this.route.snapshot.queryParamMap.get('range');
    const templateId = this.route.snapshot.queryParamMap.get('template');
    if (id) {
      this.rangeId.set(id);
      this.loadDiagram(id);
      return;
    }
    // Dialogs and overlays opened straight out of ngAfterViewInit run inside the
    // same change-detection pass that just finished, so defer by a turn.
    setTimeout(() => {
      if (templateId) {
        this.startFromTemplate(templateId);
      } else {
        this.openPicker();
      }
    });
  }

  ngOnDestroy(): void {
    if (this.snapshotTimer) clearTimeout(this.snapshotTimer);
    this.paper?.remove();
  }

  /* --- Graph + Paper init --- */
  private initGraph(): void {
    this.graph = new joint.dia.Graph();
    (this.graph as any).on('add remove', () => this.updateCounts());
  }

  private initPaper(): void {
    const el = this.canvasEl.nativeElement;
    this.paper = new joint.dia.Paper({
      el,
      model: this.graph,
      width: '100%',
      height: '100%',
      gridSize: GRID,
      drawGrid: false,
      background: { color: 'transparent' },
      defaultLink: () => new joint.shapes.standard.Link({
        attrs: {
          line: {
            stroke: 'var(--accent)',
            strokeWidth: 2,
            targetMarker: { type: 'path', d: 'M 10 -5 0 0 10 5 z', fill: 'var(--accent)' },
          },
        },
        router: { name: 'manhattan', args: { step: GRID } },
        connector: { name: 'rounded', args: { radius: 8 } },
      }),
      defaultConnectionPoint: { name: 'boundary' },
      validateConnection: (cellViewS, magnetS, cellViewT, _magnetT) => {
        return cellViewS !== cellViewT;
      },
      snapLinks: { radius: 15 },
      linkPinning: false,
      interactive: { linkMove: true },
    });
  }

  private bindEvents(): void {
    this.paper.on('element:pointerclick', (cellView: joint.dia.CellView) => {
      this.selectElement((cellView as any).model as joint.dia.Element);
    });
    this.paper.on('blank:pointerclick', () => {
      this.clearSelection();
    });
    this.paper.on('element:pointerdblclick', (cellView: joint.dia.CellView) => {
      const el = (cellView as any).model as joint.dia.Element;
      const current = String(el.attr('label/text') || '');
      this.promptText('Rename node', 'Label', current, 'Rename').subscribe(newLabel => {
        if (!newLabel) return;
        el.attr('label/text', newLabel);
        el.prop('nodeData/label', newLabel);
        if (this.selectedNode()?.id === el.id) {
          this.propLabel = newLabel;
        }
        this.scheduleSnapshot();
        this.cdr.detectChanges();
      });
    });
    (this.graph as any).on('add remove change', () => this.updateCounts());

    // History is recorded off structural events rather than off every 'change':
    // selection paints body/strokeWidth, and recording that would fill the stack
    // with entries that look identical to the author.
    (this.graph as any).on('add remove', () => this.scheduleSnapshot());
    (this.graph as any).on(
      'change:position change:size change:source change:target change:vertices',
      () => this.scheduleSnapshot(),
    );
  }

  /* --- Undo / redo ------------------------------------------------- */

  /**
   * Record the current graph, coalescing bursts. A drag emits change:position
   * per frame and an Nmap import emits one 'add' per host; without the window
   * a 50-deep stack would be consumed by a single gesture.
   */
  private scheduleSnapshot(delay = 250): void {
    if (this.suppressHistory) return;
    this.dirty.set(true);
    if (this.snapshotTimer) clearTimeout(this.snapshotTimer);
    this.snapshotTimer = setTimeout(() => {
      this.snapshotTimer = null;
      this.commitSnapshot();
    }, delay);
  }

  private commitSnapshot(): void {
    if (this.suppressHistory || !this.graph) return;
    this.history.push(this.graph.toJSON());
    this.refreshHistoryFlags();
    this.dirty.set(true);
    this.cdr.detectChanges();
  }

  /** Make the current graph the baseline: nothing before it is undoable. */
  private resetHistory(): void {
    if (this.snapshotTimer) {
      clearTimeout(this.snapshotTimer);
      this.snapshotTimer = null;
    }
    this.history.reset(this.graph ? this.graph.toJSON() : undefined);
    this.refreshHistoryFlags();
    this.dirty.set(false);
  }

  private refreshHistoryFlags(): void {
    this.canUndo.set(this.history.canUndo);
    this.canRedo.set(this.history.canRedo);
  }

  undo(): void {
    this.applySnapshot(this.history.undo(), 'Nothing to undo');
  }

  redo(): void {
    this.applySnapshot(this.history.redo(), 'Nothing to redo');
  }

  private applySnapshot(snapshot: unknown, emptyMessage: string): void {
    if (snapshot === null || snapshot === undefined) {
      this.snack.open(emptyMessage, '', { duration: 1200 });
      return;
    }
    if (this.snapshotTimer) {
      clearTimeout(this.snapshotTimer);
      this.snapshotTimer = null;
    }
    this.suppressHistory = true;
    try {
      this.graph.fromJSON(snapshot as any);
    } catch {
      this.snack.open('Could not restore that state', 'Dismiss', { duration: 3000, panelClass: 'snack-error' });
      return;
    } finally {
      this.suppressHistory = false;
    }
    this.clearSelection();
    this.updateCounts();
    this.refreshHistoryFlags();
    this.dirty.set(true);
    this.cdr.detectChanges();
  }

  /* --- Keyboard shortcuts ------------------------------------------ */

  @HostListener('document:keydown', ['$event'])
  onKeydown(e: KeyboardEvent): void {
    // A dialog owns the keyboard while it is up; Delete must not quietly remove
    // a node behind the confirm sheet that is asking about something else.
    if (this.dialog.openDialogs.length > 0) return;

    const target = e.target as HTMLElement | null;
    // Typing in a field must never be hijacked: Ctrl+Z in a text box is the
    // browser's own undo, and Backspace is a character delete.
    if (target && typeof target.closest === 'function'
        && target.closest('input,textarea,select,[contenteditable]')) {
      return;
    }
    const mod = e.ctrlKey || e.metaKey;
    const key = (e.key || '').toLowerCase();

    if (mod && key === 's') {
      e.preventDefault();
      this.saveDiagram();
      return;
    }
    if (mod && key === 'y') {
      e.preventDefault();
      this.redo();
      return;
    }
    if (mod && key === 'z') {
      e.preventDefault();
      if (e.shiftKey) this.redo();
      else this.undo();
      return;
    }
    if (mod && key === 'd') {
      e.preventDefault();
      this.duplicateSelected();
      return;
    }
    if (!mod && (e.key === 'Delete' || e.key === 'Backspace')) {
      if (!this.selectedNode()) return;
      e.preventDefault();
      this.deleteSelected();
    }
  }

  @HostListener('window:beforeunload', ['$event'])
  onBeforeUnload(e: BeforeUnloadEvent): void {
    if (!this.dirty()) return;
    e.preventDefault();
    e.returnValue = 'This range design has unsaved changes.';
  }

  /* --- Dialog helpers ---------------------------------------------- */

  private promptText(
    title: string, label: string, value: string, confirmText = 'OK',
  ): Observable<string | undefined> {
    return this.dialog.open(TextPromptDialogComponent, {
      width: '420px',
      autoFocus: true,
      data: { title, label, value, confirmText } as TextPromptData,
    }).afterClosed();
  }

  private confirm(title: string, message: string, confirmText: string): Observable<boolean> {
    return this.dialog.open(ConfirmDialogComponent, {
      width: '420px',
      data: { title, message, confirmText },
    }).afterClosed();
  }

  /* --- Drag & Drop from palette --- */
  onDragStart(event: DragEvent, item: StencilItem): void {
    this.draggedStencil = item;
    event.dataTransfer?.setData('text/plain', item.type);
  }

  onDragOver(event: DragEvent): void {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
  }

  onDrop(event: DragEvent): void {
    event.preventDefault();
    if (!this.draggedStencil) return;
    const item = this.draggedStencil;
    this.draggedStencil = null;

    const rect = this.canvasEl.nativeElement.getBoundingClientRect();
    const scale = this.paper.scale();
    const translate = this.paper.translate();
    const x = Math.round(((event.clientX - rect.left) - translate.tx) / scale.sx / GRID) * GRID;
    const y = Math.round(((event.clientY - rect.top) - translate.ty) / scale.sy / GRID) * GRID;

    const color = this.nodeColors[item.type] || '#42A5F5';

    let element: joint.dia.Element;
    if (item.type === 'subnet' || item.type === 'dmz') {
      element = createSubnetZone(item.label, x, y, 400, 300, color);
    } else {
      element = createNodeShape(item.type, item.label, item.icon, color, x, y, item.defaults);
    }

    this.graph.addCell(element);
    this.selectElement(element);
    this.scheduleSnapshot();
    this.snack.open('Added ' + item.label, '', { duration: 1500, panelClass: 'snack-success' });
  }

  /* --- Selection --- */
  private selectElement(el: joint.dia.Element): void {
    this.graph.getElements().forEach(e => {
      e.attr('body/strokeWidth', 2);
    });
    el.attr('body/strokeWidth', 4);
    this.selectedNode.set(el);
    this.selectedNodeType.set(el.prop('nodeType') || '');

    const data = el.prop('nodeData') || {};
    this.propLabel = data.label || el.attr('label/text') || '';
    this.propHostname = data.hostname || '';
    this.propIp = data.ip || '';
    this.propOs = data.os_template || '';
    this.propCpu = parseInt(data.vcpu, 10) || 2;
    this.propRam = parseInt(data.ram_mb, 10) || 2048;
    this.propDisk = parseInt(data.disk_gb, 10) || 40;
    this.propVlan = parseInt(data.vlan, 10) || 100;
    this.propCidr = data.cidr || '';
    this.propServices = data.services ? data.services.split(',').filter((s: string) => s) : [];
    this.cdr.detectChanges();
  }

  private clearSelection(): void {
    this.graph.getElements().forEach(e => e.attr('body/strokeWidth', 2));
    this.selectedNode.set(null);
    this.selectedNodeType.set('');
    this.cdr.detectChanges();
  }

  /* --- Property updates --- */
  updateNodeProp(key: string, value: string): void {
    const el = this.selectedNode();
    if (!el) return;
    if (key === 'label') {
      el.attr('label/text', value);
      el.prop('nodeData/label', value);
      // Longer window than a canvas gesture: one snapshot per edited field,
      // not one per keystroke.
      this.scheduleSnapshot(700);
    }
  }

  updateNodeData(key: string, value: string | number): void {
    const el = this.selectedNode();
    if (!el) return;
    el.prop('nodeData/' + key, String(value));
    this.scheduleSnapshot(700);
  }

  onOsChange(value: string): void {
    this.updateNodeData('os_template', value);
    if (value !== 'windows-server-2022') {
      this.propServices = [];
      const el = this.selectedNode();
      if (el) el.prop('nodeData/services', '');
    }
    this.scheduleSnapshot();
    this.cdr.detectChanges();
  }

  toggleService(serviceId: string): void {
    const idx = this.propServices.indexOf(serviceId);
    if (idx >= 0) {
      this.propServices = this.propServices.filter(s => s !== serviceId);
    } else {
      this.propServices = [...this.propServices, serviceId];
    }
    const el = this.selectedNode();
    if (el) {
      el.prop('nodeData/services', this.propServices.join(','));
      this.scheduleSnapshot();
    }
  }

  isServiceSelected(serviceId: string): boolean {
    return this.propServices.includes(serviceId);
  }

  /* --- Toolbar actions --- */
  deleteSelected(): void {
    const el = this.selectedNode();
    if (!el) return;
    el.remove();
    this.clearSelection();
    this.scheduleSnapshot();
    this.snack.open('Node removed', '', { duration: 1500 });
  }

  duplicateSelected(): void {
    const el = this.selectedNode();
    if (!el) return;
    const clone = el.clone();
    clone.translate(40, 40);
    this.graph.addCell(clone);
    this.selectElement(clone);
    this.scheduleSnapshot();
    this.snack.open('Node duplicated', '', { duration: 1500, panelClass: 'snack-success' });
  }

  zoomIn(): void {
    const s = this.paper.scale();
    this.paper.scale(Math.min(s.sx * 1.2, 3), Math.min(s.sy * 1.2, 3));
  }

  zoomOut(): void {
    const s = this.paper.scale();
    this.paper.scale(Math.max(s.sx / 1.2, 0.2), Math.max(s.sy / 1.2, 0.2));
  }

  fitContent(): void {
    this.paper.scaleContentToFit({ padding: 40, maxScale: 2 });
  }

  clearCanvas(): void {
    if (this.graph.getElements().length === 0) return;
    this.confirm(
      'Clear canvas',
      'Remove every node and link from this diagram? The change is undoable until you save.',
      'Clear',
    ).subscribe(confirmed => {
      if (!confirmed) return;
      this.graph.clear();
      this.clearSelection();
      this.scheduleSnapshot(0);
    });
  }

  /**
   * Arrange the diagram as a layered topology.
   *
   * The previous implementation placed every node on a sqrt(n) grid in insertion
   * order. It never read a single link, so a firewall could land three cells away
   * from the subnet it protects, and it excluded zones from layout while moving
   * their contents — which left subnet rectangles sitting around empty space with
   * their members scattered outside.
   *
   * This version:
   *   - ranks nodes by hop distance from the network edge (router/firewall/etc),
   *     so traffic flows top-to-bottom the way people draw a range;
   *   - keeps members of the same subnet/DMZ in a consistent horizontal band, so a
   *     zone stays a contiguous block instead of being sprayed across the canvas;
   *   - orders each rank by the average position of its already-placed neighbours,
   *     a barycentre pass that measurably reduces edge crossings;
   *   - spaces on each element's real size rather than hardcoded 180x140;
   *   - resizes zone rectangles around their members afterwards. Moving the zone to
   *     fit the nodes is always correct; trying to hold nodes inside a fixed zone is
   *     what produced orphaned rectangles before.
   */
  autoLayout(): void {
    const all = this.graph.getElements();
    const isZone = (e: joint.dia.Element) =>
      e.prop('nodeType') === 'subnet' || e.prop('nodeType') === 'dmz';
    const zones = all.filter(isZone);
    const nodes = all.filter(e => !isZone(e));

    if (nodes.length === 0) {
      this.snack.open('Nothing to lay out', '', { duration: 2000 });
      return;
    }

    // Zone membership is read from the CURRENT geometry, before anything moves —
    // once nodes are repositioned the containment that expressed the author's
    // grouping intent is gone. Smallest containing zone wins, so a host inside a
    // subnet drawn on top of a larger DMZ belongs to the subnet.
    const zoneOf = new Map<string, string>();
    for (const n of nodes) {
      const centre = n.getBBox().center();
      let best: joint.dia.Element | null = null;
      let bestArea = Infinity;
      for (const z of zones) {
        const zb = z.getBBox();
        const area = zb.width * zb.height;
        if (zb.containsPoint(centre) && area < bestArea) {
          best = z;
          bestArea = area;
        }
      }
      if (best) zoneOf.set(String(n.id), String(best.id));
    }

    // Adjacency over nodes only. Links to a zone rectangle are decoration, not
    // topology, and a self-link would otherwise pin a node to its own rank.
    const nodeIds = new Set(nodes.map(n => String(n.id)));
    const adj = new Map<string, Set<string>>();
    nodeIds.forEach(id => adj.set(id, new Set<string>()));
    for (const link of this.graph.getLinks()) {
      const s = String(link.get('source')?.id ?? '');
      const t = String(link.get('target')?.id ?? '');
      if (!s || !t || s === t || !nodeIds.has(s) || !nodeIds.has(t)) continue;
      adj.get(s)!.add(t);
      adj.get(t)!.add(s);
    }
    const degree = (id: string) => adj.get(id)?.size ?? 0;

    // Rank 0 is the network edge. Falling back to the highest-degree node keeps a
    // topology with no gateway (all-workstation lab) from ranking off an arbitrary
    // insertion-order node.
    const EDGE_TYPES = new Set(['router', 'firewall', 'internet', 'cloud', 'switch']);
    const byDegreeDesc = [...nodes].sort((a, b) => degree(String(b.id)) - degree(String(a.id)));
    const seeds = byDegreeDesc.filter(n => EDGE_TYPES.has(String(n.prop('nodeType'))));

    const rank = new Map<string, number>();
    const queue: string[] = [];
    const enqueueSeed = (id: string) => {
      if (rank.has(id)) return;
      rank.set(id, 0);
      queue.push(id);
    };
    (seeds.length ? seeds : byDegreeDesc.slice(0, 1)).forEach(n => enqueueSeed(String(n.id)));

    // BFS, restarting at rank 0 for each disconnected component so an unlinked
    // island sits alongside the topology rather than being pushed to the bottom.
    let cursor = 0;
    for (;;) {
      while (cursor < queue.length) {
        const id = queue[cursor++];
        const r = rank.get(id)!;
        for (const next of adj.get(id) ?? []) {
          if (!rank.has(next)) {
            rank.set(next, r + 1);
            queue.push(next);
          }
        }
      }
      const orphan = byDegreeDesc.find(n => !rank.has(String(n.id)));
      if (!orphan) break;
      enqueueSeed(String(orphan.id));
    }

    // A stable zone order gives each zone the same horizontal band on every rank,
    // which is what stops a zone spanning three ranks from overlapping its neighbour.
    const zoneOrder = new Map<string, number>();
    zones.forEach((z, i) => zoneOrder.set(String(z.id), i));
    const bandOf = (id: string) => {
      const z = zoneOf.get(id);
      return z === undefined ? Number.MAX_SAFE_INTEGER : (zoneOrder.get(z) ?? 0);
    };

    const ranks: joint.dia.Element[][] = [];
    for (const n of nodes) {
      const r = rank.get(String(n.id)) ?? 0;
      (ranks[r] ??= []).push(n);
    }

    const HGAP = 48;
    const VGAP = 90;
    const order = new Map<string, number>();

    ranks.forEach((row, r) => {
      if (!row) return;
      const previous = r > 0 ? (ranks[r - 1] ?? []) : [];
      const barycentre = (el: joint.dia.Element) => {
        const neighbours = [...(adj.get(String(el.id)) ?? [])]
          .map(id => order.get(id))
          .filter((v): v is number => v !== undefined);
        if (neighbours.length === 0) return Number.MAX_SAFE_INTEGER;
        return neighbours.reduce((a, b) => a + b, 0) / neighbours.length;
      };
      previous.forEach((el, i) => order.set(String(el.id), i));

      row.sort((a, b) => {
        const bandDiff = bandOf(String(a.id)) - bandOf(String(b.id));
        if (bandDiff !== 0) return bandDiff;
        const bcDiff = barycentre(a) - barycentre(b);
        if (bcDiff !== 0) return bcDiff;
        // Final tiebreak on label so a redraw of the same diagram is identical.
        return String(a.attr('label/text') ?? '').localeCompare(String(b.attr('label/text') ?? ''));
      });
      row.forEach((el, i) => order.set(String(el.id), i));
    });

    // Widest rank sets the centre line; every other rank is centred against it.
    const rowWidth = (row: joint.dia.Element[]) =>
      row.reduce((sum, el) => sum + el.size().width, 0) + HGAP * Math.max(0, row.length - 1);
    const widest = Math.max(...ranks.filter(Boolean).map(rowWidth));

    let y = 60;
    ranks.forEach(row => {
      if (!row || row.length === 0) return;
      let x = 60 + (widest - rowWidth(row)) / 2;
      let tallest = 0;
      for (const el of row) {
        const { width, height } = el.size();
        el.position(Math.round(x), Math.round(y));
        x += width + HGAP;
        tallest = Math.max(tallest, height);
      }
      y += tallest + VGAP;
    });

    // Zones follow their contents. A zone that lost every member keeps its size —
    // silently shrinking an empty subnet to nothing would look like data loss.
    const ZONE_PAD = 28;
    const ZONE_LABEL_SPACE = 22;
    for (const z of zones) {
      const members = nodes.filter(n => zoneOf.get(String(n.id)) === String(z.id));
      if (members.length === 0) continue;
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const m of members) {
        const b = m.getBBox();
        minX = Math.min(minX, b.x);
        minY = Math.min(minY, b.y);
        maxX = Math.max(maxX, b.x + b.width);
        maxY = Math.max(maxY, b.y + b.height);
      }
      z.position(Math.round(minX - ZONE_PAD), Math.round(minY - ZONE_PAD - ZONE_LABEL_SPACE));
      z.resize(
        Math.round(maxX - minX + ZONE_PAD * 2),
        Math.round(maxY - minY + ZONE_PAD * 2 + ZONE_LABEL_SPACE),
      );
      z.toBack();
    }

    this.paper.scaleContentToFit({ padding: 40, maxScale: 1.5 });
    this.scheduleSnapshot();
    this.snack.open('Layout applied', '', { duration: 1500, panelClass: 'snack-success' });
  }

  /* --- Save / Load persisted diagram --- */
  saveDiagram(): void {
    const id = this.rangeId();
    if (!id) {
      this.snack.open('Open the designer with ?range=<id> to enable save', '', { duration: 3000 });
      return;
    }
    const graphJson = this.graph.toJSON();
    this.api.saveRangeDiagram(id, graphJson).subscribe({
      next: () => {
        this.dirty.set(false);
        this.cdr.detectChanges();
        this.snack.open('Diagram saved', '', { duration: 1500, panelClass: 'snack-success' });
      },
      error: (err) => this.snack.open(err?.error?.detail || 'Save failed', 'Dismiss', { duration: 4000, panelClass: 'snack-error' }),
    });
  }

  loadDiagram(id: string): void {
    // The description travels with the range, not the diagram, so it needs its
    // own read. A failure here must not stop the topology from loading.
    this.api.getRange(id).subscribe({
      next: r => this.rangeDescription.set(r.description || ''),
      error: () => this.rangeDescription.set(''),
    });
    this.api.getRangeDiagram(id).subscribe({
      next: (res) => {
        const diagram = res?.diagram_json;
        if (!diagram || !diagram.cells || !diagram.cells.length) {
          this.snack.open('No saved diagram for this range — starting blank', '', { duration: 2500 });
          return;
        }
        // A load is a new baseline, not an edit: the restored cells must not
        // arrive on the stack as something to undo, nor mark the range dirty.
        this.suppressHistory = true;
        try {
          this.graph.fromJSON(diagram);
        } catch {
          this.snack.open('Saved diagram was corrupted — starting blank', 'Dismiss', { duration: 4000, panelClass: 'snack-error' });
          return;
        } finally {
          this.suppressHistory = false;
        }
        this.resetHistory();
        // Non-fatal cosmetics: a fit/scale hiccup must not blank an otherwise-valid diagram.
        try { this.updateCounts(); } catch { /* ignore */ }
        try { this.paper.scaleContentToFit({ padding: 40, maxScale: 1.5 }); } catch { /* ignore */ }
        this.snack.open('Diagram loaded', '', { duration: 1500, panelClass: 'snack-success' });
      },
      error: (err) => this.snack.open(err?.error?.detail || 'Load failed', 'Dismiss', { duration: 4000, panelClass: 'snack-error' }),
    });
  }

  /* --- Golden-image OS catalogue ----------------------------------- */

  /**
   * Replace the hard-coded OS list with aliases the hypervisor has actually
   * verified. An empty map or a failed call keeps the fallback: offering no OS
   * at all would be worse than offering one that might not be built yet.
   */
  private loadOsOptions(): void {
    this.api.getGoldenImageAliasMap().subscribe({
      next: (map) => {
        const keys = Object.keys(map || {});
        if (keys.length === 0) return;
        this.osOptions.set(keys.map(k => ({ value: k, label: k + ' → ' + map[k] })));
        this.cdr.detectChanges();
      },
      error: () => { /* keep FALLBACK_OS_OPTIONS */ },
    });
  }

  /* --- Range / template picker ------------------------------------- */

  private openPicker(): void {
    this.showPicker.set(true);
    this.cdr.detectChanges();
    this.api.listRanges().subscribe({
      next: (rows) => { this.ranges.set(rows || []); this.cdr.detectChanges(); },
      error: () => { /* the blank-canvas path still works without a list */ },
    });
    this.api.listTemplates().subscribe({
      next: (rows) => { this.templates.set(rows || []); this.cdr.detectChanges(); },
      error: () => { /* as above */ },
    });
  }

  dismissPicker(): void {
    this.showPicker.set(false);
    this.cdr.detectChanges();
  }

  openSelectedRange(): void {
    const id = this.pickerRangeId;
    if (!id) return;
    this.dismissPicker();
    this.rangeId.set(id);
    this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { range: id, template: null },
      queryParamsHandling: 'merge',
    });
    this.loadDiagram(id);
  }

  startFromSelectedTemplate(): void {
    if (!this.pickerTemplateId) return;
    this.startFromTemplate(this.pickerTemplateId);
  }

  /**
   * Create a range from a template and seed the canvas with the template's
   * starter topology. Used both by the picker and by an entry on ?template=.
   */
  private startFromTemplate(templateId: string): void {
    const known = this.templates().find(t => t.id === templateId);
    if (known) {
      this.promptForRangeName(templateId, known.name);
      return;
    }
    this.api.getTemplate(templateId).subscribe({
      next: (t) => this.promptForRangeName(templateId, t?.name || 'New range'),
      error: () => this.promptForRangeName(templateId, 'New range'),
    });
  }

  private promptForRangeName(templateId: string, defaultName: string): void {
    this.promptText('Name the range', 'Range name', defaultName, 'Create')
      .subscribe(name => {
        if (!name) return;
        this.createRangeFromTemplate(templateId, name);
      });
  }

  private createRangeFromTemplate(templateId: string, name: string): void {
    this.api.createRange({ name, template_id: templateId }).subscribe({
      next: (range) => {
        this.dismissPicker();
        this.rangeId.set(range.id);
        this.router.navigate([], {
          relativeTo: this.route,
          queryParams: { range: range.id, template: null },
          queryParamsHandling: 'merge',
        });
        this.seedFromTemplateDiagram(templateId);
      },
      error: (err) => this.snack.open(
        err?.error?.detail || 'Could not create the range', 'Dismiss',
        { duration: 4000, panelClass: 'snack-error' },
      ),
    });
  }

  private seedFromTemplateDiagram(templateId: string): void {
    this.api.templateDiagramPreview(templateId).subscribe({
      next: (res) => {
        const diagram = res?.diagram_json;
        if (diagram && diagram.cells && diagram.cells.length) {
          this.suppressHistory = true;
          try {
            this.graph.fromJSON(diagram);
          } catch {
            this.snack.open('Template preview was unusable — starting blank', '', { duration: 3000 });
          } finally {
            this.suppressHistory = false;
          }
          try { this.updateCounts(); } catch { /* ignore */ }
          try { this.paper.scaleContentToFit({ padding: 40, maxScale: 1.5 }); } catch { /* ignore */ }
        }
        this.resetHistory();
        // Persist immediately so the new range is not an empty row if the tab
        // is closed before the author touches anything.
        this.saveDiagram();
      },
      error: () => {
        this.resetHistory();
        this.snack.open('Range created — template had no starter topology', '', { duration: 3000 });
      },
    });
  }

  /* --- YAML Export --- */
  exportYaml(): void {
    const elements = this.graph.getElements();
    const links = this.graph.getLinks();
    const lines: string[] = [
      '# TrueNorth Range Template - exported from Range Designer',
      '# Generated: ' + new Date().toISOString(),
      'id: range-design-export',
      'name: "Range Design Export"',
      'version: "1.0"',
      '',
      'nodes:',
    ];
    for (const el of elements) {
      const data = el.prop('nodeData') || {};
      const nodeType = el.prop('nodeType') || 'unknown';
      const pos = el.position();
      lines.push('  - id: ' + el.id);
      lines.push('    type: ' + nodeType);
      lines.push('    label: "' + (data.label || '') + '"');
      lines.push('    position: { x: ' + pos.x + ', y: ' + pos.y + ' }');
      if (data.hostname) lines.push('    hostname: "' + data.hostname + '"');
      if (data.ip) lines.push('    ip: "' + data.ip + '"');
      if (data.os_template) lines.push('    os_template: ' + data.os_template);
      if (data.vcpu) lines.push('    vcpu: ' + data.vcpu);
      if (data.ram_mb) lines.push('    ram_mb: ' + data.ram_mb);
      if (data.disk_gb) lines.push('    disk_gb: ' + data.disk_gb);
      if (data.vlan) lines.push('    vlan: ' + data.vlan);
      if (data.services) {
        const svcs = data.services.split(',').filter((s: string) => s);
        if (svcs.length > 0) {
          lines.push('    services:');
          for (const s of svcs) {
            lines.push('      - ' + s);
          }
        }
      }
      if (data.cidr) lines.push('    cidr: "' + data.cidr + '"');
      lines.push('');
    }
    if (links.length > 0) {
      lines.push('links:');
      for (const link of links) {
        const src = link.source();
        const tgt = link.target();
        if (src.id && tgt.id) {
          lines.push('  - source: ' + src.id);
          lines.push('    target: ' + tgt.id);
          if (src.port) lines.push('    source_port: ' + src.port);
          if (tgt.port) lines.push('    target_port: ' + tgt.port);
          lines.push('');
        }
      }
    }

    const yaml = lines.join('\n');
    const blob = new Blob([yaml], { type: 'text/yaml' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'range-design.yaml';
    a.click();
    URL.revokeObjectURL(url);
    this.snack.open('YAML exported', '', { duration: 2000, panelClass: 'snack-success' });
  }

  importYaml(): void {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.yaml,.yml';
    input.onchange = (e: Event) => {
      const file = (e.target as HTMLInputElement).files?.[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        this.snack.open('Imported ' + file.name + ' - parsing not yet implemented', '', { duration: 3000 });
      };
      reader.readAsText(file);
    };
    input.click();
  }

  /* --- Nmap XML Import --- */
  importNmap(): void {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.xml';
    input.onchange = (e: Event) => {
      const file = (e.target as HTMLInputElement).files?.[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        try {
          this.parseNmapXml(reader.result as string, file.name);
        } catch (err) {
          this.snack.open('Failed to parse nmap XML: ' + (err as Error).message, 'OK', { duration: 5000 });
        }
      };
      reader.readAsText(file);
    };
    input.click();
  }

  private parseNmapXml(xml: string, filename: string): void {
    const parser = new DOMParser();
    const doc = parser.parseFromString(xml, 'text/xml');

    const parseError = doc.querySelector('parsererror');
    if (parseError) {
      this.snack.open('Invalid XML file', 'OK', { duration: 3000 });
      return;
    }

    const hosts = doc.querySelectorAll('host');
    if (hosts.length === 0) {
      this.snack.open('No hosts found in nmap scan', 'OK', { duration: 3000 });
      return;
    }

    /* Service-to-Windows-role mapping */
    const svcMap: Record<string, string> = {
      'microsoft-ds': 'ad-ds', 'ldap': 'ad-ds', 'kerberos': 'ad-ds',
      'domain': 'dns', 'dns': 'dns',
      'dhcps': 'dhcp', 'dhcp': 'dhcp',
      'http': 'iis', 'https': 'iis', 'http-proxy': 'iis',
      'ms-sql-s': 'sql-server', 'ms-sql-m': 'sql-server', 'mysql': 'sql-server',
      'smtp': 'exchange', 'pop3': 'exchange', 'imap': 'exchange',
      'msrpc': '',  /* too generic */
      'netbios-ssn': '',
      'rdp': 'rds', 'ms-wbt-server': 'rds',
      'ssh': '',
      'ftp': 'file-print',
      'smb': 'file-print',
    };

    let created = 0;
    const COLS = Math.ceil(Math.sqrt(hosts.length));

    hosts.forEach((host, idx) => {
      /* Skip hosts that are down */
      const status = host.querySelector('status');
      if (status && status.getAttribute('state') !== 'up') return;

      /* IP address */
      const addrEl = host.querySelector('address[addrtype="ipv4"]')
                  || host.querySelector('address');
      const ip = addrEl ? (addrEl.getAttribute('addr') || '') : '';

      /* Hostname */
      const hostnameEl = host.querySelector('hostnames hostname');
      const hostname = hostnameEl ? (hostnameEl.getAttribute('name') || '') : '';

      /* OS detection */
      const osMatch = host.querySelector('osmatch');
      const osName = osMatch ? (osMatch.getAttribute('name') || '').toLowerCase() : '';
      const osFamilyEl = host.querySelector('osclass');
      const osFamily = osFamilyEl ? (osFamilyEl.getAttribute('osfamily') || '').toLowerCase() : '';
      const osType = osFamilyEl ? (osFamilyEl.getAttribute('type') || '').toLowerCase() : '';

      /* Ports & services */
      const ports = host.querySelectorAll('port');
      const openPorts: Array<{port: number; proto: string; service: string; product: string}> = [];
      ports.forEach(p => {
        const state = p.querySelector('state');
        if (!state || state.getAttribute('state') !== 'open') return;
        const svcEl = p.querySelector('service');
        openPorts.push({
          port: parseInt(p.getAttribute('portid') || '0', 10),
          proto: p.getAttribute('protocol') || 'tcp',
          service: svcEl ? (svcEl.getAttribute('name') || '') : '',
          product: svcEl ? (svcEl.getAttribute('product') || '') : '',
        });
      });

      /* Determine node type and OS template */
      let nodeType = 'server';
      let osTemplate = 'ubuntu-22.04';
      const label = hostname || ip || 'Host ' + (idx + 1);
      const detectedServices: string[] = [];

      /* OS classification */
      if (osFamily.includes('windows') || osName.includes('windows')) {
        if (osName.includes('server') || osType === 'general purpose') {
          /* Check if it looks like a domain controller */
          const hasDcPorts = openPorts.some(p =>
            p.service === 'ldap' || p.service === 'kerberos' || p.port === 389 || p.port === 88
          );
          if (hasDcPorts) {
            nodeType = 'dc';
            osTemplate = 'windows-server-2022';
          } else {
            nodeType = 'server';
            osTemplate = 'windows-server-2022';
          }
        } else {
          nodeType = 'workstation';
          osTemplate = 'windows-11';
        }
      } else if (osFamily.includes('linux') || osName.includes('linux') || osName.includes('ubuntu')) {
        if (osName.includes('kali')) {
          nodeType = 'kali';
          osTemplate = 'kali-2024';
        } else if (osName.includes('rocky') || osName.includes('centos') || osName.includes('rhel')) {
          nodeType = 'server';
          osTemplate = 'rocky-9';
        } else {
          nodeType = 'server';
          osTemplate = 'ubuntu-22.04';
        }
      } else if (osName.includes('pfsense') || osName.includes('freebsd')) {
        nodeType = 'firewall';
        osTemplate = 'pfsense';
      } else if (osName.includes('vyos') || osName.includes('vyatta')) {
        nodeType = 'router';
        osTemplate = 'vyos';
      }

      /* Heuristic: if no OS detected, guess from services */
      if (!osMatch) {
        const hasWindowsSvc = openPorts.some(p =>
          p.service === 'microsoft-ds' || p.service === 'ms-wbt-server' || p.product.toLowerCase().includes('microsoft')
        );
        const hasLinuxSvc = openPorts.some(p =>
          p.service === 'ssh' && !hasWindowsSvc
        );
        if (hasWindowsSvc) {
          nodeType = 'server';
          osTemplate = 'windows-server-2022';
        } else if (hasLinuxSvc) {
          nodeType = 'server';
          osTemplate = 'ubuntu-22.04';
        }
      }

      /* Map open services to Windows Server roles */
      if (osTemplate === 'windows-server-2022') {
        openPorts.forEach(p => {
          const role = svcMap[p.service];
          if (role && !detectedServices.includes(role)) {
            detectedServices.push(role);
          }
          /* Port-based fallbacks */
          if (p.port === 88 && !detectedServices.includes('ad-ds')) detectedServices.push('ad-ds');
          if (p.port === 389 && !detectedServices.includes('ad-ds')) detectedServices.push('ad-ds');
          if (p.port === 636 && !detectedServices.includes('ad-ds')) detectedServices.push('ad-ds');
          if (p.port === 53 && !detectedServices.includes('dns')) detectedServices.push('dns');
          if (p.port === 67 && !detectedServices.includes('dhcp')) detectedServices.push('dhcp');
          if (p.port === 80 && !detectedServices.includes('iis')) detectedServices.push('iis');
          if (p.port === 443 && !detectedServices.includes('iis')) detectedServices.push('iis');
          if (p.port === 1433 && !detectedServices.includes('sql-server')) detectedServices.push('sql-server');
          if (p.port === 25 && !detectedServices.includes('exchange')) detectedServices.push('exchange');
          if (p.port === 587 && !detectedServices.includes('exchange')) detectedServices.push('exchange');
          if (p.port === 3389 && !detectedServices.includes('rds')) detectedServices.push('rds');
          if (p.port === 445 && !detectedServices.includes('file-print')) detectedServices.push('file-print');
        });
      }

      /* Grid position */
      const col = created % COLS;
      const row = Math.floor(created / COLS);
      const x = 60 + col * 180;
      const y = 60 + row * 140;

      const color = this.nodeColors[nodeType] || '#42A5F5';
      const extra: Record<string, string> = {
        os_template: osTemplate,
        ip: ip,
        hostname: hostname,
        vcpu: nodeType === 'workstation' ? '2' : '4',
        ram_mb: nodeType === 'workstation' ? '4096' : '8192',
        disk_gb: nodeType === 'workstation' ? '60' : '100',
      };
      if (detectedServices.length > 0) {
        extra['services'] = detectedServices.join(',');
      }

      /* Truncate label for display */
      const displayLabel = label.length > 18 ? label.substring(0, 15) + '...' : label;

      const element = createNodeShape(nodeType, displayLabel, '', color, x, y, extra);
      this.graph.addCell(element);
      created++;
    });

    if (created > 0) {
      this.paper.scaleContentToFit({ padding: 40, maxScale: 1.5 });
      this.updateCounts();
      // One entry for the whole import, not one per host.
      this.scheduleSnapshot();
    }
    this.snack.open(
      'Imported ' + created + ' hosts from ' + filename +
      (hosts.length > created ? ' (' + (hosts.length - created) + ' down/skipped)' : ''),
      'OK',
      { duration: 5000, panelClass: 'snack-success' }
    );
  }

  private updateCounts(): void {
    this.elementCount.set(this.graph.getElements().length);
    this.linkCount.set(this.graph.getLinks().length);
    this.cdr.detectChanges();
  }
}