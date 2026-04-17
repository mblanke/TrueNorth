import {
  Component,
  OnDestroy,
  ElementRef,
  ViewChild,
  AfterViewInit,
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
import * as joint from 'jointjs';
import { FilterCategoryPipe } from './filter-category.pipe';

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
        fontFamily: 'Calibri, Segoe UI, sans-serif',
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
        fontFamily: 'Calibri, Segoe UI, sans-serif',
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

@Component({
  selector: 'tn-range-designer',
  standalone: true,
  encapsulation: ViewEncapsulation.None,
  imports: [
    CommonModule, FormsModule, MatCardModule, MatButtonModule, MatIconModule,
    MatFormFieldModule, MatInputModule, MatSelectModule, MatCheckboxModule, MatTooltipModule,
    MatSliderModule, MatDividerModule, MatSnackBarModule, FilterCategoryPipe,
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
          <span class="canvas-title">Range Topology</span>
          <span class="canvas-info">{{ elementCount() }} nodes &middot; {{ linkCount() }} links</span>
          <span class="spacer"></span>
          <button mat-stroked-button (click)="autoLayout()">
            <mat-icon>auto_fix_high</mat-icon> Auto Layout
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
        <div #canvas class="joint-canvas"></div>
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
                  <option value="ubuntu-22.04">Ubuntu 22.04</option>
                  <option value="ubuntu-24.04">Ubuntu 24.04</option>
                  <option value="windows-server-2022">Windows Server 2022</option>
                  <option value="windows-11">Windows 11</option>
                  <option value="kali-2024">Kali Linux 2024</option>
                  <option value="rocky-9">Rocky Linux 9</option>
                  <option value="security-onion">Security Onion</option>
                  <option value="pfsense">pfSense</option>
                  <option value="vyos">VyOS</option>
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
      </aside>
    </div>

    <!-- Hidden textarea for YAML import -->
    <textarea #yamlInput style="position:absolute;left:-9999px;"></textarea>
  `,
  styles: [`
    :host { display: block; height: calc(100vh - 64px); overflow: hidden; }

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
    .joint-canvas {
      flex: 1;
      background:
        linear-gradient(var(--border) 1px, transparent 1px),
        linear-gradient(90deg, var(--border) 1px, transparent 1px);
      background-size: 20px 20px;
      background-color: var(--bg-primary);
      cursor: crosshair;
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
      font-family: 'Calibri', 'Segoe UI', sans-serif !important;
      background: transparent !important;
      cursor: pointer;
    }
    :host ::ng-deep .properties select[matNativeControl] option {
      background: #163764;
      color: #F0F4F8;
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

  constructor(private cdr: ChangeDetectorRef, private snack: MatSnackBar) {}

  ngAfterViewInit(): void {
    this.initGraph();
    this.initPaper();
    this.bindEvents();
  }

  ngOnDestroy(): void {
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
      const current = el.attr('label/text') || '';
      const newLabel = prompt('Rename node:', current);
      if (newLabel !== null && newLabel.trim()) {
        el.attr('label/text', newLabel.trim());
        el.prop('nodeData/label', newLabel.trim());
        if (this.selectedNode()?.id === el.id) {
          this.propLabel = newLabel.trim();
          this.cdr.detectChanges();
        }
      }
    });
    (this.graph as any).on('add remove change', () => this.updateCounts());
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
    }
  }

  updateNodeData(key: string, value: string | number): void {
    const el = this.selectedNode();
    if (!el) return;
    el.prop('nodeData/' + key, String(value));
  }

  onOsChange(value: string): void {
    this.updateNodeData('os_template', value);
    if (value !== 'windows-server-2022') {
      this.propServices = [];
      const el = this.selectedNode();
      if (el) el.prop('nodeData/services', '');
    }
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
    this.snack.open('Node removed', '', { duration: 1500 });
  }

  duplicateSelected(): void {
    const el = this.selectedNode();
    if (!el) return;
    const clone = el.clone();
    clone.translate(40, 40);
    this.graph.addCell(clone);
    this.selectElement(clone);
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
    if (confirm('Clear all nodes and links?')) {
      this.graph.clear();
      this.clearSelection();
    }
  }

  autoLayout(): void {
    const elements = this.graph.getElements().filter(
      e => e.prop('nodeType') !== 'subnet' && e.prop('nodeType') !== 'dmz'
    );
    const cols = Math.ceil(Math.sqrt(elements.length));
    elements.forEach((el, i) => {
      const col = i % cols;
      const row = Math.floor(i / cols);
      el.position(60 + col * 180, 60 + row * 140);
    });
    this.paper.scaleContentToFit({ padding: 40, maxScale: 1.5 });
    this.snack.open('Layout applied', '', { duration: 1500, panelClass: 'snack-success' });
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