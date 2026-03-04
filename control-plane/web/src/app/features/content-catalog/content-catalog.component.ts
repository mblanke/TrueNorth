import { Component, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatChipsModule } from '@angular/material/chips';
import { MatTabsModule } from '@angular/material/tabs';

interface ContentItem {
  name: string;
  type: string;
  description: string;
  difficulty?: string;
  mitre?: string[];
}

@Component({
  selector: 'tn-content-catalog',
  standalone: true,
  imports: [CommonModule, MatCardModule, MatButtonModule, MatIconModule, MatChipsModule, MatTabsModule],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">library_books</mat-icon>
          <div>
            <h1>Content Catalog</h1>
            <p class="subtitle">Browse available range templates, scenarios, inject packs, and detection rules.</p>
          </div>
        </div>
      </div>

      <mat-tab-group>
        <mat-tab label="Range Templates">
          <div class="card-grid mt-2">
            @for (item of rangeTemplates; track item.name) {
              <mat-card>
                <mat-card-header>
                  <mat-icon mat-card-avatar>description</mat-icon>
                  <mat-card-title>{{ item.name }}</mat-card-title>
                  <mat-card-subtitle>{{ item.difficulty }}</mat-card-subtitle>
                </mat-card-header>
                <mat-card-content><p>{{ item.description }}</p></mat-card-content>
                <mat-card-actions><button mat-button color="primary">Use Template</button></mat-card-actions>
              </mat-card>
            }
          </div>
        </mat-tab>

        <mat-tab label="Inject Packs">
          <div class="card-grid mt-2">
            @for (item of injectPacks; track item.name) {
              <mat-card>
                <mat-card-header>
                  <mat-icon mat-card-avatar>bug_report</mat-icon>
                  <mat-card-title>{{ item.name }}</mat-card-title>
                </mat-card-header>
                <mat-card-content>
                  <p>{{ item.description }}</p>
                  <mat-chip-set>
                    @for (t of item.mitre || []; track t) {
                      <mat-chip>{{ t }}</mat-chip>
                    }
                  </mat-chip-set>
                </mat-card-content>
              </mat-card>
            }
          </div>
        </mat-tab>

        <mat-tab label="Detection Rules">
          <div class="card-grid mt-2">
            @for (item of detectionRules; track item.name) {
              <mat-card>
                <mat-card-header>
                  <mat-icon mat-card-avatar>shield</mat-icon>
                  <mat-card-title>{{ item.name }}</mat-card-title>
                </mat-card-header>
                <mat-card-content>
                  <p>{{ item.description }}</p>
                  <mat-chip-set>
                    @for (t of item.mitre || []; track t) {
                      <mat-chip>{{ t }}</mat-chip>
                    }
                  </mat-chip-set>
                </mat-card-content>
              </mat-card>
            }
          </div>
        </mat-tab>
      </mat-tab-group>
    </div>
  `,
  styles: [],
})
export class ContentCatalogComponent {
  rangeTemplates: ContentItem[] = [
    { name: 'Small Enterprise', type: 'template', description: 'Basic office network with DC, workstation, and file server.', difficulty: 'Beginner' },
    { name: 'Medium Enterprise', type: 'template', description: 'Multi-subnet enterprise with DMZ, mail, SIEM, and firewall.', difficulty: 'Intermediate' },
  ];
  injectPacks: ContentItem[] = [
    { name: 'Phishing Campaign', type: 'inject', description: 'Spear-phishing with credential harvesting and lateral movement.', mitre: ['T1566.001', 'T1078', 'T1021.001'] },
    { name: 'Ransomware Simulation', type: 'inject', description: 'Full ransomware chain from macro to encryption.', mitre: ['T1059.001', 'T1486', 'T1490'] },
    { name: 'Insider Threat', type: 'inject', description: 'Data exfiltration by malicious insider using legitimate tools.', mitre: ['T1078', 'T1567.002', 'T1070.001'] },
  ];
  detectionRules: ContentItem[] = [
    { name: 'PowerShell Download Cradle', type: 'detection', description: 'Detects PowerShell download commands.', mitre: ['T1059.001', 'T1105'] },
    { name: 'VSS Deletion', type: 'detection', description: 'Detects shadow copy deletion (ransomware indicator).', mitre: ['T1490'] },
    { name: 'Suspicious DNS (DGA)', type: 'detection', description: 'High-entropy DNS queries indicating C2.', mitre: ['T1568.002'] },
    { name: 'RDP Brute Force', type: 'detection', description: 'Multiple failed RDP logons from single source.', mitre: ['T1110.001'] },
  ];
}
