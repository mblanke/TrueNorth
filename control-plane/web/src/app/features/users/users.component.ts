import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { MatTabsModule } from '@angular/material/tabs';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTableModule } from '@angular/material/table';
import { MatChipsModule } from '@angular/material/chips';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatBadgeModule } from '@angular/material/badge';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MatDividerModule } from '@angular/material/divider';

interface Nation {
  id: string; name: string; iso_alpha2: string; iso_alpha3: string;
  flag_emoji: string; is_nato: boolean; is_fvey: boolean;
}
interface Coalition {
  id: string; name: string; slug: string; description: string | null;
}
interface UserFull {
  id: string; email: string; display_name: string; role: string;
  first_name: string | null; last_name: string | null; rank: string | null;
  service_branch: string | null; nation_id: string | null;
  clearance_level: string; unit: string | null; callsign: string | null;
  source: string;
}
interface TeamFull {
  id: string; name: string; team_type: string; color_hex: string | null;
  description: string | null; max_members: number | null; is_persistent: boolean;
}
interface OUTree {
  id: string; name: string; slug: string; ou_type: string;
  children: OUTree[];
}
interface SecurityGroup {
  id: string; name: string; slug: string; group_type: string;
  description: string | null; created_at: string;
}
interface ADSyncStatus {
  connected: boolean; last_sync_at: string | null;
  users_synced: number; groups_synced: number; errors: string[];
}
interface AuthZone {
  id: string; zone_name: string; description: string | null;
  allowed_methods: string; require_mfa: boolean;
  session_timeout_minutes: number; max_failed_attempts: number;
  clearance_required: string; is_active: boolean;
}

@Component({
  selector: 'app-users',
  standalone: true,
  imports: [
    CommonModule, FormsModule, MatTabsModule, MatCardModule, MatButtonModule,
    MatIconModule, MatTableModule, MatChipsModule, MatFormFieldModule,
    MatInputModule, MatSelectModule, MatTooltipModule, MatExpansionModule,
    MatBadgeModule, MatSnackBarModule, MatDividerModule,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <mat-icon class="page-icon">shield</mat-icon>
          <div>
            <h1>Users & Directory</h1>
            <p class="subtitle">Personnel management, teams, nations, OUs, security groups, and AD sync</p>
          </div>
        </div>
      </div>

      <mat-tab-group>
        <!-- ===== Personnel Tab ===== -->
        <mat-tab label="Personnel">
          <div class="tab-content">
            <div class="tab-toolbar">
              <mat-form-field appearance="outline" class="search-field" subscriptSizing="dynamic">
                <mat-label>Search</mat-label>
                <input matInput [(ngModel)]="userSearch" placeholder="Name, email, callsign...">
                <mat-icon matSuffix>search</mat-icon>
              </mat-form-field>
              <button mat-raised-button color="primary" (click)="editingUserId ? cancelUserEdit() : (showUserForm = !showUserForm)">
                <mat-icon>person_add</mat-icon> {{ (showUserForm || editingUserId) ? 'Cancel' : 'Add User' }}
              </button>
            </div>

            <!-- Add / Edit User Form -->
            <mat-card *ngIf="showUserForm || editingUserId" class="add-form-card">
              <mat-card-header><mat-card-title>{{ editingUserId ? 'Edit User' : 'New User' }}</mat-card-title></mat-card-header>
              <mat-card-content>
                <div class="form-row">
                  <mat-form-field appearance="outline">
                    <mat-label>Email</mat-label>
                    <input matInput [(ngModel)]="newUser.email" placeholder="user@example.com" [readonly]="!!editingUserId">
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Display Name</mat-label>
                    <input matInput [(ngModel)]="newUser.display_name" placeholder="John Doe">
                  </mat-form-field>
                </div>
                <div class="form-row">
                  <mat-form-field appearance="outline">
                    <mat-label>First Name</mat-label>
                    <input matInput [(ngModel)]="newUser.first_name">
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Last Name</mat-label>
                    <input matInput [(ngModel)]="newUser.last_name">
                  </mat-form-field>
                </div>
                <div class="form-row">
                  <mat-form-field appearance="outline">
                    <mat-label>Role</mat-label>
                    <mat-select [(ngModel)]="newUser.role" panelClass="tn-select-panel">
                      <mat-option value="student">Student</mat-option>
                      <mat-option value="instructor">Instructor</mat-option>
                      <mat-option value="observer">Observer</mat-option>
                      <mat-option value="range_ops">Range Ops</mat-option>
                      <mat-option value="admin">Admin</mat-option>
                    </mat-select>
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Clearance Level</mat-label>
                    <mat-select [(ngModel)]="newUser.clearance_level" panelClass="tn-select-panel">
                      <mat-option value="unclassified">Unclassified</mat-option>
                      <mat-option value="protected">Protected</mat-option>
                      <mat-option value="confidential">Confidential</mat-option>
                      <mat-option value="secret">Secret</mat-option>
                      <mat-option value="top_secret">Top Secret</mat-option>
                    </mat-select>
                  </mat-form-field>
                </div>
                <div class="form-row">
                  <mat-form-field appearance="outline">
                    <mat-label>Rank</mat-label>
                    <input matInput [(ngModel)]="newUser.rank" placeholder="e.g. CPT, SGT, Civ">
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Service Branch</mat-label>
                    <input matInput [(ngModel)]="newUser.service_branch" placeholder="e.g. Army, Navy">
                  </mat-form-field>
                </div>
                <div class="form-row">
                  <mat-form-field appearance="outline">
                    <mat-label>Nation</mat-label>
                    <mat-select [(ngModel)]="newUser.nation_id" panelClass="tn-select-panel">
                      <mat-option [value]="null">-- None --</mat-option>
                      <mat-option *ngFor="let n of nations" [value]="n.id">
                        {{ n.flag_emoji }} {{ n.name }}
                      </mat-option>
                    </mat-select>
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Unit</mat-label>
                    <input matInput [(ngModel)]="newUser.unit" placeholder="e.g. 1st Cyber Bn">
                  </mat-form-field>
                </div>
                <div class="form-row">
                  <mat-form-field appearance="outline">
                    <mat-label>Callsign</mat-label>
                    <input matInput [(ngModel)]="newUser.callsign" placeholder="e.g. Viper">
                  </mat-form-field>
                </div>
              </mat-card-content>
              <mat-card-actions>
                <button mat-raised-button color="primary" (click)="editingUserId ? updateUser() : createUser()" [disabled]="userSaving || !newUser.email || !newUser.display_name">
                  <mat-icon>save</mat-icon> {{ editingUserId ? 'Save Changes' : 'Create User' }}
                </button>
                <button mat-button (click)="editingUserId ? cancelUserEdit() : (showUserForm = false)">Cancel</button>
              </mat-card-actions>
            </mat-card>

            <table mat-table [dataSource]="filteredUsers" class="full-width">
              <ng-container matColumnDef="name">
                <th mat-header-cell *matHeaderCellDef>Name</th>
                <td mat-cell *matCellDef="let u">
                  <strong>{{ u.display_name }}</strong>
                  <span *ngIf="u.rank" class="rank-badge">{{ u.rank }}</span>
                  <span *ngIf="u.callsign" class="callsign">"{{ u.callsign }}"</span>
                </td>
              </ng-container>
              <ng-container matColumnDef="email">
                <th mat-header-cell *matHeaderCellDef>Email</th>
                <td mat-cell *matCellDef="let u">{{ u.email }}</td>
              </ng-container>
              <ng-container matColumnDef="role">
                <th mat-header-cell *matHeaderCellDef>Role</th>
                <td mat-cell *matCellDef="let u">
                  <mat-chip-set><mat-chip [class]="'role-' + u.role">{{ u.role }}</mat-chip></mat-chip-set>
                </td>
              </ng-container>
              <ng-container matColumnDef="clearance">
                <th mat-header-cell *matHeaderCellDef>Clearance</th>
                <td mat-cell *matCellDef="let u">
                  <mat-chip-set><mat-chip [class]="'cl-' + u.clearance_level">{{ u.clearance_level }}</mat-chip></mat-chip-set>
                </td>
              </ng-container>
              <ng-container matColumnDef="source">
                <th mat-header-cell *matHeaderCellDef>Source</th>
                <td mat-cell *matCellDef="let u">{{ u.source }}</td>
              </ng-container>
              <ng-container matColumnDef="actions">
                <th mat-header-cell *matHeaderCellDef></th>
                <td mat-cell *matCellDef="let u">
                  <button mat-icon-button matTooltip="Edit" (click)="startEditUser(u)">
                    <mat-icon>edit</mat-icon>
                  </button>
                  <button mat-icon-button color="warn" matTooltip="Delete" (click)="deleteUser(u)">
                    <mat-icon>delete</mat-icon>
                  </button>
                </td>
              </ng-container>
              <tr mat-header-row *matHeaderRowDef="userColumns"></tr>
              <tr mat-row *matRowDef="let row; columns: userColumns;"></tr>
            </table>
            <p *ngIf="filteredUsers.length === 0" class="empty-state">No users found.</p>
          </div>
        </mat-tab>

        <!-- ===== Teams Tab ===== -->
        <mat-tab label="Teams">
          <div class="tab-content">
            <div class="tab-toolbar">
              <span class="toolbar-spacer"></span>
              <button mat-raised-button color="primary" (click)="editingTeamId ? cancelTeamEdit() : (showTeamForm = !showTeamForm)">
                <mat-icon>group_add</mat-icon> {{ (showTeamForm || editingTeamId) ? 'Cancel' : 'Add Team' }}
              </button>
            </div>

            <!-- Add / Edit Team Form -->
            <mat-card *ngIf="showTeamForm || editingTeamId" class="add-form-card">
              <mat-card-header><mat-card-title>{{ editingTeamId ? 'Edit Team' : 'New Team' }}</mat-card-title></mat-card-header>
              <mat-card-content>
                <div class="form-row">
                  <mat-form-field appearance="outline">
                    <mat-label>Team Name</mat-label>
                    <input matInput [(ngModel)]="newTeam.name" placeholder="Alpha Team">
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Type</mat-label>
                    <mat-select [(ngModel)]="newTeam.team_type" panelClass="tn-select-panel">
                      <mat-option value="red">Red</mat-option>
                      <mat-option value="blue">Blue</mat-option>
                      <mat-option value="white">White</mat-option>
                      <mat-option value="green">Green</mat-option>
                      <mat-option value="purple">Purple</mat-option>
                      <mat-option value="custom">Custom</mat-option>
                    </mat-select>
                  </mat-form-field>
                </div>
                <div class="form-row">
                  <mat-form-field appearance="outline">
                    <mat-label>Description</mat-label>
                    <input matInput [(ngModel)]="newTeam.description">
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Max Members</mat-label>
                    <input matInput type="number" [(ngModel)]="newTeam.max_members">
                  </mat-form-field>
                </div>
                <div class="form-row">
                  <mat-form-field appearance="outline">
                    <mat-label>Color Hex</mat-label>
                    <input matInput [(ngModel)]="newTeam.color_hex" placeholder="#FF0000">
                  </mat-form-field>
                </div>
              </mat-card-content>
              <mat-card-actions>
                <button mat-raised-button color="primary" (click)="editingTeamId ? updateTeam() : createTeam()" [disabled]="teamSaving || !newTeam.name">
                  <mat-icon>save</mat-icon> {{ editingTeamId ? 'Save Changes' : 'Create Team' }}
                </button>
                <button mat-button (click)="editingTeamId ? cancelTeamEdit() : (showTeamForm = false)">Cancel</button>
              </mat-card-actions>
            </mat-card>

            <div class="teams-grid">
              <mat-card *ngFor="let t of teams" class="team-card" [style.border-left-color]="t.color_hex || 'var(--accent)'">
                <mat-card-header>
                  <mat-card-title>{{ t.name }}</mat-card-title>
                  <mat-card-subtitle *ngIf="t.team_type">{{ t.team_type | uppercase }} Team</mat-card-subtitle>
                </mat-card-header>
                <mat-card-content>
                  <p *ngIf="t.description">{{ t.description }}</p>
                  <div class="team-meta">
                    <span *ngIf="t.max_members"><mat-icon>group</mat-icon> Max: {{ t.max_members }}</span>
                    <span><mat-icon>{{ t.is_persistent ? 'lock' : 'lock_open' }}</mat-icon> {{ t.is_persistent ? 'Persistent' : 'Temporary' }}</span>
                  </div>
                </mat-card-content>
                <mat-card-actions>
                  <button mat-icon-button matTooltip="Edit" (click)="startEditTeam(t)">
                    <mat-icon>edit</mat-icon>
                  </button>
                  <button mat-icon-button color="warn" matTooltip="Delete" (click)="deleteTeam(t)">
                    <mat-icon>delete</mat-icon>
                  </button>
                </mat-card-actions>
              </mat-card>
            </div>
            <p *ngIf="teams.length === 0" class="empty-state">No teams configured.</p>
          </div>
        </mat-tab>

        <!-- ===== Nations Tab ===== -->
        <mat-tab label="Nations ({{ nations.length }})">
          <div class="tab-content">
            <div class="nations-grid">
              <mat-card *ngFor="let n of nations" class="nation-card">
                <div class="nation-flag">{{ n.flag_emoji }}</div>
                <div class="nation-info">
                  <strong>{{ n.name }}</strong>
                  <span class="nation-code">{{ n.iso_alpha3 }}</span>
                </div>
                <div class="nation-badges">
                  <mat-chip-set>
                    <mat-chip *ngIf="n.is_nato" class="badge-nato">NATO</mat-chip>
                    <mat-chip *ngIf="n.is_fvey" class="badge-fvey">FVEY</mat-chip>
                  </mat-chip-set>
                </div>
              </mat-card>
            </div>
          </div>
        </mat-tab>

        <!-- ===== Coalitions Tab ===== -->
        <mat-tab label="Coalitions">
          <div class="tab-content">
            <mat-card *ngFor="let c of coalitions" class="coalition-card">
              <mat-card-header>
                <mat-card-title>{{ c.name }}</mat-card-title>
                <mat-card-subtitle>{{ c.slug }}</mat-card-subtitle>
              </mat-card-header>
              <mat-card-content>
                <p>{{ c.description }}</p>
              </mat-card-content>
            </mat-card>
          </div>
        </mat-tab>

        <!-- ===== Directory Tab ===== -->
        <mat-tab label="Directory">
          <div class="tab-content">
            <div class="tab-toolbar">
              <h3 style="margin: 0;">Organizational Units</h3>
              <button mat-raised-button color="accent" (click)="editingOuId ? cancelOuEdit() : (showOUForm = !showOUForm)">
                <mat-icon>create_new_folder</mat-icon> {{ (showOUForm || editingOuId) ? 'Cancel' : 'Add OU' }}
              </button>
            </div>

            <!-- Add / Edit OU Form -->
            <mat-card *ngIf="showOUForm || editingOuId" class="add-form-card">
              <mat-card-header><mat-card-title>{{ editingOuId ? 'Edit OU' : 'New OU' }}</mat-card-title></mat-card-header>
              <mat-card-content>
                <div class="form-row">
                  <mat-form-field appearance="outline">
                    <mat-label>OU Name</mat-label>
                    <input matInput [(ngModel)]="newOU.name" placeholder="HQ Division">
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Slug</mat-label>
                    <input matInput [(ngModel)]="newOU.slug" placeholder="hq-division">
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Type</mat-label>
                    <mat-select [(ngModel)]="newOU.ou_type" panelClass="tn-select-panel">
                      <mat-option value="department">Department</mat-option>
                      <mat-option value="division">Division</mat-option>
                      <mat-option value="team">Team</mat-option>
                      <mat-option value="custom">Custom</mat-option>
                    </mat-select>
                  </mat-form-field>
                </div>
              </mat-card-content>
              <mat-card-actions>
                <button mat-raised-button color="primary" (click)="editingOuId ? updateOu() : createOU()" [disabled]="ouSaving || !newOU.name || !newOU.slug">
                  <mat-icon>save</mat-icon> {{ editingOuId ? 'Save Changes' : 'Create OU' }}
                </button>
                <button mat-button (click)="editingOuId ? cancelOuEdit() : (showOUForm = false)">Cancel</button>
              </mat-card-actions>
            </mat-card>

            <div *ngFor="let ou of ouTree" class="ou-node">
              <mat-icon>folder</mat-icon>
              <strong>{{ ou.name }}</strong> ({{ ou.ou_type }})
              <button mat-icon-button matTooltip="Edit" (click)="startEditOu(ou)">
                <mat-icon>edit</mat-icon>
              </button>
              <div *ngFor="let child of ou.children" class="ou-child">
                <mat-icon>subdirectory_arrow_right</mat-icon>
                {{ child.name }} ({{ child.ou_type }})
                <button mat-icon-button matTooltip="Edit" (click)="startEditOu(child)">
                  <mat-icon>edit</mat-icon>
                </button>
              </div>
            </div>
            <p *ngIf="ouTree.length === 0" class="empty-state">No OUs configured.</p>

            <mat-divider style="margin: 24px 0;"></mat-divider>

            <div class="tab-toolbar">
              <h3 style="margin: 0;">Security Groups</h3>
              <button mat-raised-button color="accent" (click)="editingGroupId ? cancelGroupEdit() : (showGroupForm = !showGroupForm)">
                <mat-icon>security</mat-icon> {{ (showGroupForm || editingGroupId) ? 'Cancel' : 'Add Group' }}
              </button>
            </div>

            <!-- Add / Edit Security Group Form -->
            <mat-card *ngIf="showGroupForm || editingGroupId" class="add-form-card">
              <mat-card-header><mat-card-title>{{ editingGroupId ? 'Edit Group' : 'New Group' }}</mat-card-title></mat-card-header>
              <mat-card-content>
                <div class="form-row">
                  <mat-form-field appearance="outline">
                    <mat-label>Group Name</mat-label>
                    <input matInput [(ngModel)]="newGroup.name" placeholder="Range Operators">
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Slug</mat-label>
                    <input matInput [(ngModel)]="newGroup.slug" placeholder="range-operators">
                  </mat-form-field>
                  <mat-form-field appearance="outline">
                    <mat-label>Type</mat-label>
                    <mat-select [(ngModel)]="newGroup.group_type" panelClass="tn-select-panel">
                      <mat-option value="access">Access</mat-option>
                      <mat-option value="role">Role</mat-option>
                      <mat-option value="distribution">Distribution</mat-option>
                    </mat-select>
                  </mat-form-field>
                </div>
                <mat-form-field appearance="outline" class="full-width">
                  <mat-label>Description</mat-label>
                  <input matInput [(ngModel)]="newGroup.description">
                </mat-form-field>
              </mat-card-content>
              <mat-card-actions>
                <button mat-raised-button color="primary" (click)="editingGroupId ? updateGroup() : createGroup()" [disabled]="groupSaving || !newGroup.name || !newGroup.slug">
                  <mat-icon>save</mat-icon> {{ editingGroupId ? 'Save Changes' : 'Create Group' }}
                </button>
                <button mat-button (click)="editingGroupId ? cancelGroupEdit() : (showGroupForm = false)">Cancel</button>
              </mat-card-actions>
            </mat-card>

            <table mat-table [dataSource]="securityGroups" class="full-width">
              <ng-container matColumnDef="name">
                <th mat-header-cell *matHeaderCellDef>Name</th>
                <td mat-cell *matCellDef="let g">{{ g.name }}</td>
              </ng-container>
              <ng-container matColumnDef="type">
                <th mat-header-cell *matHeaderCellDef>Type</th>
                <td mat-cell *matCellDef="let g">{{ g.group_type }}</td>
              </ng-container>
              <ng-container matColumnDef="description">
                <th mat-header-cell *matHeaderCellDef>Description</th>
                <td mat-cell *matCellDef="let g">{{ g.description || '-' }}</td>
              </ng-container>
              <ng-container matColumnDef="actions">
                <th mat-header-cell *matHeaderCellDef></th>
                <td mat-cell *matCellDef="let g">
                  <button mat-icon-button matTooltip="Edit" (click)="startEditGroup(g)">
                    <mat-icon>edit</mat-icon>
                  </button>
                </td>
              </ng-container>
              <tr mat-header-row *matHeaderRowDef="['name','type','description','actions']"></tr>
              <tr mat-row *matRowDef="let row; columns: ['name','type','description','actions'];"></tr>
            </table>
          </div>
        </mat-tab>

        <!-- ===== AD Sync Tab ===== -->
        <mat-tab label="AD Sync">
          <div class="tab-content">
            <mat-card class="sync-card" *ngIf="adSyncStatus">
              <mat-card-header>
                <mat-card-title>
                  <mat-icon [class]="adSyncStatus.connected ? 'status-online' : 'status-offline'">
                    {{ adSyncStatus.connected ? 'cloud_done' : 'cloud_off' }}
                  </mat-icon>
                  AD/LDAP Federation
                </mat-card-title>
              </mat-card-header>
              <mat-card-content>
                <div class="sync-stats">
                  <div><strong>Users Synced:</strong> {{ adSyncStatus.users_synced }}</div>
                  <div><strong>Groups Synced:</strong> {{ adSyncStatus.groups_synced }}</div>
                  <div><strong>Last Sync:</strong> {{ adSyncStatus.last_sync_at || 'Never' }}</div>
                </div>
              </mat-card-content>
              <mat-card-actions>
                <button mat-raised-button color="primary" (click)="triggerSync()">
                  <mat-icon>sync</mat-icon> Trigger Sync
                </button>
              </mat-card-actions>
            </mat-card>
          </div>
        </mat-tab>

        <!-- ===== Auth Zones Tab ===== -->
        <mat-tab label="Auth Zones">
          <div class="tab-content">
            <ng-container *ngFor="let z of authZones">
              <mat-card class="zone-card">
                <mat-card-header>
                  <mat-card-title>{{ z.zone_name }}</mat-card-title>
                </mat-card-header>
                <mat-card-content>
                  <p>{{ z.description }}</p>
                  <div class="zone-info">
                    <span><strong>Methods:</strong> {{ z.allowed_methods }}</span>
                    <span><strong>MFA Required:</strong> {{ z.require_mfa ? 'Yes' : 'No' }}</span>
                    <span><strong>Timeout:</strong> {{ z.session_timeout_minutes }}min</span>
                    <span><strong>Clearance:</strong> {{ z.clearance_required }}</span>
                  </div>
                </mat-card-content>
                <mat-card-actions>
                  <button mat-icon-button matTooltip="Edit" (click)="startEditZone(z)">
                    <mat-icon>edit</mat-icon>
                  </button>
                </mat-card-actions>
              </mat-card>

              <!-- Auth Zone Inline Edit Form -->
              <mat-card *ngIf="editingZoneId === z.id" class="add-form-card">
                <mat-card-header><mat-card-title>Edit Auth Zone</mat-card-title></mat-card-header>
                <mat-card-content>
                  <div class="form-row">
                    <mat-form-field appearance="outline">
                      <mat-label>Zone Name</mat-label>
                      <input matInput [(ngModel)]="zoneForm.zone_name">
                    </mat-form-field>
                    <mat-form-field appearance="outline">
                      <mat-label>Description</mat-label>
                      <input matInput [(ngModel)]="zoneForm.description">
                    </mat-form-field>
                  </div>
                  <div class="form-row">
                    <mat-form-field appearance="outline">
                      <mat-label>Allowed Methods</mat-label>
                      <input matInput [(ngModel)]="zoneForm.allowed_methods" placeholder="password,cac,token">
                    </mat-form-field>
                    <mat-form-field appearance="outline">
                      <mat-label>Clearance Required</mat-label>
                      <mat-select [(ngModel)]="zoneForm.clearance_required" panelClass="tn-select-panel">
                        <mat-option value="unclassified">Unclassified</mat-option>
                        <mat-option value="protected">Protected</mat-option>
                        <mat-option value="confidential">Confidential</mat-option>
                        <mat-option value="secret">Secret</mat-option>
                        <mat-option value="top_secret">Top Secret</mat-option>
                      </mat-select>
                    </mat-form-field>
                  </div>
                  <div class="form-row">
                    <mat-form-field appearance="outline">
                      <mat-label>Require MFA</mat-label>
                      <mat-select [(ngModel)]="zoneForm.require_mfa" panelClass="tn-select-panel">
                        <mat-option [value]="true">Yes</mat-option>
                        <mat-option [value]="false">No</mat-option>
                      </mat-select>
                    </mat-form-field>
                    <mat-form-field appearance="outline">
                      <mat-label>Session Timeout (min)</mat-label>
                      <input matInput type="number" [(ngModel)]="zoneForm.session_timeout_minutes">
                    </mat-form-field>
                    <mat-form-field appearance="outline">
                      <mat-label>Max Failed Attempts</mat-label>
                      <input matInput type="number" [(ngModel)]="zoneForm.max_failed_attempts">
                    </mat-form-field>
                  </div>
                </mat-card-content>
                <mat-card-actions>
                  <button mat-raised-button color="primary" (click)="updateZone()" [disabled]="zoneSaving || !zoneForm.zone_name">
                    <mat-icon>save</mat-icon> Save Changes
                  </button>
                  <button mat-button (click)="cancelZoneEdit()">Cancel</button>
                </mat-card-actions>
              </mat-card>
            </ng-container>
          </div>
        </mat-tab>
      </mat-tab-group>
    </div>
  `,
  styles: [`
        .page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
    .header-left { display: flex; align-items: center; gap: 16px; }

    h1 { margin: 0; font-size: 24px; color: var(--text-primary); }
    .subtitle { margin: 4px 0 0; color: var(--text-secondary); font-size: 14px; }
    .tab-content { padding: 16px 0; }
    .tab-toolbar { display: flex; align-items: center; gap: 16px; margin-bottom: 16px; }
    .toolbar-spacer { flex: 1; }
    .full-width { width: 100%; }
    .search-field { width: 400px; }
    .add-form-card { margin-bottom: 16px; background: var(--bg-card); border: 1px solid var(--accent); }
    .form-row { display: flex; gap: 16px; margin-bottom: 8px; }
    .form-row mat-form-field { flex: 1; }
    .rank-badge { background: var(--accent); color: #fff; padding: 2px 8px; border-radius: 3px; font-size: 11px; margin-left: 8px; }
    .callsign { color: var(--text-secondary); font-style: italic; margin-left: 8px; }
    .teams-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
    .team-card { background: var(--bg-card); border-left: 4px solid var(--accent); }
    .team-meta { display: flex; gap: 16px; margin-top: 8px; color: var(--text-secondary); font-size: 13px; }
    .team-meta mat-icon { font-size: 16px; width: 16px; height: 16px; vertical-align: middle; }
    .nations-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; }
    .nation-card { display: flex; align-items: center; gap: 12px; padding: 12px 16px; background: var(--bg-card); border: 1px solid var(--border); }
    .nation-flag { font-size: 32px; }
    .nation-info { flex: 1; }
    .nation-info strong { display: block; color: var(--text-primary); }
    .nation-code { font-size: 12px; color: var(--text-secondary); }
    .badge-nato { background: #1565c0 !important; color: #fff !important; }
    .badge-fvey { background: #7b1fa2 !important; color: #fff !important; }
    .coalition-card { margin-bottom: 12px; background: var(--bg-card); border: 1px solid var(--border); }
    .ou-node { padding: 8px 0 8px 16px; display: flex; align-items: center; gap: 8px; color: var(--text-primary); flex-wrap: wrap; }
    .ou-child { padding: 4px 0 4px 40px; display: flex; align-items: center; gap: 8px; color: var(--text-secondary); }
    .sync-card { max-width: 600px; background: var(--bg-card); }
    .sync-stats { display: flex; flex-direction: column; gap: 8px; margin: 16px 0; }
    .zone-card { margin-bottom: 12px; background: var(--bg-card); border: 1px solid var(--border); }
    .zone-info { display: flex; flex-wrap: wrap; gap: 24px; margin-top: 12px; color: var(--text-secondary); }
    .status-online { color: #4caf50; vertical-align: middle; margin-right: 8px; }
    .status-offline { color: #f44336; vertical-align: middle; margin-right: 8px; }
    .empty-state { text-align: center; padding: 40px; color: var(--text-secondary); }
    table { background: transparent !important; }
    th, td { color: var(--text-primary) !important; }
  `],
})
export class UsersComponent implements OnInit {
  users: UserFull[] = [];
  teams: TeamFull[] = [];
  nations: Nation[] = [];
  coalitions: Coalition[] = [];
  ouTree: OUTree[] = [];
  securityGroups: SecurityGroup[] = [];
  adSyncStatus: ADSyncStatus | null = null;
  authZones: AuthZone[] = [];
  userSearch = '';
  userColumns = ['name', 'email', 'role', 'clearance', 'source', 'actions'];

  // Form visibility toggles
  showUserForm = false;
  showTeamForm = false;
  showOUForm = false;
  showGroupForm = false;

  // New entity form models
  newUser: any = this.emptyUser();
  newTeam: any = { name: '', team_type: 'blue', description: '', max_members: 10, color_hex: '#2196F3' };
  newOU: any = { name: '', slug: '', ou_type: 'department' };
  newGroup: any = { name: '', slug: '', group_type: 'access', description: '' };

  // Edit mode state
  editingUserId: string | null = null;
  userSaving = false;
  editingTeamId: string | null = null;
  teamSaving = false;
  editingZoneId: string | null = null;
  zoneForm: any = {};
  zoneSaving = false;
  editingOuId: string | null = null;
  ouSaving = false;
  editingGroupId: string | null = null;
  groupSaving = false;

  constructor(private http: HttpClient, private snack: MatSnackBar) {}

  emptyUser(): any {
    return {
      email: '', display_name: '', role: 'student', clearance_level: 'unclassified',
      first_name: '', last_name: '', rank: '', service_branch: '',
      nation_id: null, unit: '', callsign: '',
    };
  }

  get filteredUsers(): UserFull[] {
    if (!this.userSearch) return this.users;
    const s = this.userSearch.toLowerCase();
    return this.users.filter(u =>
      u.display_name.toLowerCase().includes(s) ||
      u.email.toLowerCase().includes(s) ||
      (u.callsign || '').toLowerCase().includes(s)
    );
  }

  ngOnInit(): void {
    this.loadUsers();
    this.loadTeams();
    this.http.get<Nation[]>('/api/directory/nations').subscribe({ next: n => this.nations = n, error: () => {} });
    this.http.get<Coalition[]>('/api/directory/coalitions').subscribe({ next: c => this.coalitions = c, error: () => {} });
    this.http.get<OUTree[]>('/api/directory/ous/tree').subscribe({ next: t => this.ouTree = t, error: () => {} });
    this.http.get<SecurityGroup[]>('/api/directory/groups').subscribe({ next: g => this.securityGroups = g, error: () => {} });
    this.http.get<ADSyncStatus>('/api/ad-sync/status').subscribe({ next: s => this.adSyncStatus = s, error: () => {} });
    this.http.get<AuthZone[]>('/api/auth-zones').subscribe({ next: z => this.authZones = z, error: () => {} });
  }

  loadUsers(): void {
    this.http.get<any>('/api/users').subscribe({
      next: (data: any) => { if (Array.isArray(data)) this.users = data; },
      error: () => {},
    });
  }

  loadTeams(): void {
    this.http.get<any>('/api/teams').subscribe({
      next: (data: any) => { if (Array.isArray(data)) this.teams = data; },
      error: () => {},
    });
  }

  // -- User CRUD --
  createUser(): void {
    this.http.post('/api/users', this.newUser).subscribe({
      next: () => {
        this.snack.open('User created', '', { duration: 2000, panelClass: 'snack-success' });
        this.newUser = this.emptyUser();
        this.showUserForm = false;
        this.loadUsers();
      },
      error: (err: any) => {
        this.snack.open(err.error?.detail || 'Failed to create user', 'OK', { duration: 5000 });
      },
    });
  }

  startEditUser(u: UserFull): void {
    this.editingUserId = u.id;
    this.showUserForm = false;
    this.newUser = {
      email: u.email,
      display_name: u.display_name,
      role: u.role,
      first_name: u.first_name || '',
      last_name: u.last_name || '',
      rank: u.rank || '',
      service_branch: u.service_branch || '',
      nation_id: u.nation_id,
      clearance_level: u.clearance_level,
      unit: u.unit || '',
      callsign: u.callsign || '',
    };
  }

  updateUser(): void {
    if (!this.editingUserId) return;
    this.userSaving = true;
    this.http.patch('/api/users/' + this.editingUserId, this.newUser).subscribe({
      next: () => {
        this.userSaving = false;
        this.snack.open('User updated', '', { duration: 2000, panelClass: 'snack-success' });
        this.cancelUserEdit();
        this.loadUsers();
      },
      error: (err: any) => {
        this.userSaving = false;
        this.snack.open(err.error?.detail || 'Failed to update user', 'OK', { duration: 5000 });
      },
    });
  }

  cancelUserEdit(): void {
    this.editingUserId = null;
    this.newUser = this.emptyUser();
    this.showUserForm = false;
  }

  deleteUser(u: UserFull): void {
    if (!confirm('Delete user "' + u.display_name + '"? This cannot be undone.')) return;
    this.http.delete('/api/users/' + u.id).subscribe({
      next: () => {
        this.snack.open('User deleted', '', { duration: 2000 });
        this.loadUsers();
      },
      error: (err: any) => {
        this.snack.open(err.error?.detail || 'Failed to delete user', 'OK', { duration: 5000 });
      },
    });
  }

  // -- Team CRUD --
  createTeam(): void {
    this.http.post('/api/teams', this.newTeam).subscribe({
      next: () => {
        this.snack.open('Team created', '', { duration: 2000, panelClass: 'snack-success' });
        this.newTeam = { name: '', team_type: 'blue', description: '', max_members: 10, color_hex: '#2196F3' };
        this.showTeamForm = false;
        this.loadTeams();
      },
      error: (err: any) => {
        this.snack.open(err.error?.detail || 'Failed to create team', 'OK', { duration: 5000 });
      },
    });
  }

  startEditTeam(t: TeamFull): void {
    this.editingTeamId = t.id;
    this.showTeamForm = false;
    this.newTeam = {
      name: t.name,
      team_type: t.team_type,
      description: t.description || '',
      max_members: t.max_members || 10,
      color_hex: t.color_hex || '#2196F3',
    };
  }

  updateTeam(): void {
    if (!this.editingTeamId) return;
    this.teamSaving = true;
    this.http.patch('/api/teams/' + this.editingTeamId, this.newTeam).subscribe({
      next: () => {
        this.teamSaving = false;
        this.snack.open('Team updated', '', { duration: 2000, panelClass: 'snack-success' });
        this.cancelTeamEdit();
        this.loadTeams();
      },
      error: (err: any) => {
        this.teamSaving = false;
        this.snack.open(err.error?.detail || 'Failed to update team', 'OK', { duration: 5000 });
      },
    });
  }

  cancelTeamEdit(): void {
    this.editingTeamId = null;
    this.newTeam = { name: '', team_type: 'blue', description: '', max_members: 10, color_hex: '#2196F3' };
    this.showTeamForm = false;
  }

  deleteTeam(t: TeamFull): void {
    if (!confirm('Delete team "' + t.name + '"?')) return;
    this.http.delete('/api/teams/' + t.id).subscribe({
      next: () => {
        this.snack.open('Team deleted', '', { duration: 2000 });
        this.loadTeams();
      },
      error: (err: any) => {
        this.snack.open(err.error?.detail || 'Failed to delete team', 'OK', { duration: 5000 });
      },
    });
  }

  // -- OU CRUD --
  createOU(): void {
    this.http.post('/api/directory/ous', this.newOU).subscribe({
      next: () => {
        this.snack.open('OU created', '', { duration: 2000, panelClass: 'snack-success' });
        this.newOU = { name: '', slug: '', ou_type: 'department' };
        this.showOUForm = false;
        this.http.get<OUTree[]>('/api/directory/ous/tree').subscribe({ next: t => this.ouTree = t, error: () => {} });
      },
      error: (err: any) => {
        this.snack.open(err.error?.detail || 'Failed to create OU', 'OK', { duration: 5000 });
      },
    });
  }

  startEditOu(ou: OUTree): void {
    this.editingOuId = ou.id;
    this.showOUForm = false;
    this.newOU = {
      name: ou.name,
      slug: ou.slug,
      ou_type: ou.ou_type,
    };
  }

  updateOu(): void {
    if (!this.editingOuId) return;
    this.ouSaving = true;
    this.http.patch('/api/directory/ous/' + this.editingOuId, this.newOU).subscribe({
      next: () => {
        this.ouSaving = false;
        this.snack.open('OU updated', '', { duration: 2000, panelClass: 'snack-success' });
        this.cancelOuEdit();
        this.http.get<OUTree[]>('/api/directory/ous/tree').subscribe({ next: t => this.ouTree = t, error: () => {} });
      },
      error: (err: any) => {
        this.ouSaving = false;
        this.snack.open(err.error?.detail || 'Failed to update OU', 'OK', { duration: 5000 });
      },
    });
  }

  cancelOuEdit(): void {
    this.editingOuId = null;
    this.newOU = { name: '', slug: '', ou_type: 'department' };
    this.showOUForm = false;
  }

  // -- Security Group CRUD --
  createGroup(): void {
    this.http.post('/api/directory/groups', this.newGroup).subscribe({
      next: () => {
        this.snack.open('Security group created', '', { duration: 2000, panelClass: 'snack-success' });
        this.newGroup = { name: '', slug: '', group_type: 'access', description: '' };
        this.showGroupForm = false;
        this.http.get<SecurityGroup[]>('/api/directory/groups').subscribe({ next: g => this.securityGroups = g, error: () => {} });
      },
      error: (err: any) => {
        this.snack.open(err.error?.detail || 'Failed to create group', 'OK', { duration: 5000 });
      },
    });
  }

  startEditGroup(g: SecurityGroup): void {
    this.editingGroupId = g.id;
    this.showGroupForm = false;
    this.newGroup = {
      name: g.name,
      slug: g.slug,
      group_type: g.group_type,
      description: g.description || '',
    };
  }

  updateGroup(): void {
    if (!this.editingGroupId) return;
    this.groupSaving = true;
    this.http.patch('/api/directory/groups/' + this.editingGroupId, this.newGroup).subscribe({
      next: () => {
        this.groupSaving = false;
        this.snack.open('Security group updated', '', { duration: 2000, panelClass: 'snack-success' });
        this.cancelGroupEdit();
        this.http.get<SecurityGroup[]>('/api/directory/groups').subscribe({ next: g => this.securityGroups = g, error: () => {} });
      },
      error: (err: any) => {
        this.groupSaving = false;
        this.snack.open(err.error?.detail || 'Failed to update group', 'OK', { duration: 5000 });
      },
    });
  }

  cancelGroupEdit(): void {
    this.editingGroupId = null;
    this.newGroup = { name: '', slug: '', group_type: 'access', description: '' };
    this.showGroupForm = false;
  }

  // -- Auth Zone Edit --
  startEditZone(z: AuthZone): void {
    this.editingZoneId = z.id;
    this.zoneForm = {
      zone_name: z.zone_name,
      description: z.description || '',
      allowed_methods: z.allowed_methods,
      require_mfa: z.require_mfa,
      session_timeout_minutes: z.session_timeout_minutes,
      max_failed_attempts: z.max_failed_attempts || 5,
      clearance_required: z.clearance_required,
    };
  }

  updateZone(): void {
    if (!this.editingZoneId) return;
    this.zoneSaving = true;
    this.http.patch('/api/auth-zones/' + this.editingZoneId, this.zoneForm).subscribe({
      next: () => {
        this.zoneSaving = false;
        this.snack.open('Auth zone updated', '', { duration: 2000, panelClass: 'snack-success' });
        this.cancelZoneEdit();
        this.http.get<AuthZone[]>('/api/auth-zones').subscribe({ next: z => this.authZones = z, error: () => {} });
      },
      error: (err: any) => {
        this.zoneSaving = false;
        this.snack.open(err.error?.detail || 'Failed to update auth zone', 'OK', { duration: 5000 });
      },
    });
  }

  cancelZoneEdit(): void {
    this.editingZoneId = null;
    this.zoneForm = {};
    this.zoneSaving = false;
  }

  // -- AD Sync --
  triggerSync(): void {
    this.http.post<any>('/api/ad-sync/trigger', {}).subscribe({
      next: (r: any) => {
        this.snack.open(r.message || 'Sync triggered', '', { duration: 3000, panelClass: 'snack-success' });
      },
      error: (err: any) => {
        this.snack.open(err.error?.detail || 'Sync failed', 'OK', { duration: 5000 });
      },
    });
  }
}
