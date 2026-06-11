import { Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule, ReactiveFormsModule, FormBuilder, FormGroup, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTabsModule } from '@angular/material/tabs';
import { MatTableModule } from '@angular/material/table';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { MatDividerModule } from '@angular/material/divider';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatChipsModule } from '@angular/material/chips';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatExpansionModule } from '@angular/material/expansion';
import { ApiService } from '@core/services/api.service';
import { NotificationService } from '@core/services/notification.service';
import { HttpClient } from '@angular/common/http';
import { LottieIconComponent } from '../../shared/components/lottie-icon.component';

interface Course {
  id: string;
  name: string;
  description: string;
  difficulty: string;
  duration_hours: number;
  is_published: boolean;
  tags: string | string[];
  modules: any[];
}

interface LearningPath {
  id: string;
  name: string;
  description: string;
  course_ids: string[];
}

interface Enrollment {
  id: string;
  user_id: string;
  course_id: string;
  enrolled_at: string;
  progress_pct: number;
  user_name?: string;
  course_name?: string;
}

interface UserSummary {
  id: string;
  display_name: string;
  email: string;
}

interface TranscriptEntry {
  source: string;
  activity_type: string;
  title: string;
  score: number | null;
  grade: string | null;
  completed_at: string | null;
}

interface Certification {
  cert_name: string;
  issuer: string;
  credential_id: string;
  issued_at: string;
  expires_at: string | null;
  status: string;
}

interface Transcript {
  entries: TranscriptEntry[];
  certifications: Certification[];
  total_hours: number;
}

@Component({
  selector: 'tn-training',
  standalone: true,
  imports: [
    CommonModule, FormsModule, ReactiveFormsModule,
    MatCardModule, MatButtonModule, MatIconModule, MatTabsModule,
    MatTableModule, MatFormFieldModule, MatInputModule, MatSelectModule,
    MatSlideToggleModule, MatDividerModule, MatProgressBarModule,
    MatChipsModule, MatTooltipModule, MatExpansionModule, LottieIconComponent,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <tn-lottie name="shield-pulse" [size]="64" />
          <div>
            <h1>LMS Administration</h1>
            <p class="subtitle">Manage courses, learning paths, enrollments, and individual training records.</p>
          </div>
        </div>
        <div class="header-actions">
          @if (activeTab() === 0) {
            <button mat-raised-button color="primary" (click)="openCourseForm()">
              <mat-icon>add</mat-icon> New Course
            </button>
          }
          @if (activeTab() === 1) {
            <button mat-raised-button color="primary" (click)="openPathForm()">
              <mat-icon>add</mat-icon> New Learning Path
            </button>
          }
        </div>
      </div>

      <mat-tab-group (selectedIndexChange)="activeTab.set($event)">

        <!-- ── TAB 1: COURSES ── -->
        <mat-tab label="Courses">
          <div class="tab-content">

            <!-- Inline course form -->
            @if (showCourseForm()) {
              <mat-card class="form-card mb-2">
                <mat-card-header>
                  <mat-card-title>{{ editingCourse() ? 'Edit Course' : 'New Course' }}</mat-card-title>
                </mat-card-header>
                <mat-card-content>
                  <form [formGroup]="courseForm" class="form-grid">
                    <mat-form-field appearance="outline">
                      <mat-label>Course Name</mat-label>
                      <input matInput formControlName="name" placeholder="e.g. Incident Response Fundamentals" />
                    </mat-form-field>
                    <mat-form-field appearance="outline">
                      <mat-label>Difficulty</mat-label>
                      <mat-select panelClass="tn-select-panel" formControlName="difficulty">
                        <mat-option value="beginner">Beginner</mat-option>
                        <mat-option value="intermediate">Intermediate</mat-option>
                        <mat-option value="advanced">Advanced</mat-option>
                        <mat-option value="expert">Expert</mat-option>
                      </mat-select>
                    </mat-form-field>
                    <mat-form-field appearance="outline">
                      <mat-label>Duration (hours)</mat-label>
                      <input matInput type="number" formControlName="duration_hours" min="1" />
                    </mat-form-field>
                    <mat-form-field appearance="outline">
                      <mat-label>Tags (comma-separated)</mat-label>
                      <input matInput formControlName="tags" placeholder="e.g. soc, dfir, network" />
                    </mat-form-field>
                    <mat-form-field appearance="outline" class="full-span">
                      <mat-label>Description</mat-label>
                      <textarea matInput formControlName="description" rows="3"></textarea>
                    </mat-form-field>
                    <div class="toggle-row full-span">
                      <mat-slide-toggle formControlName="is_published" color="primary">
                        Published (visible to trainees)
                      </mat-slide-toggle>
                    </div>
                  </form>
                </mat-card-content>
                <mat-card-actions align="end">
                  <button mat-button (click)="cancelCourseForm()">Cancel</button>
                  <button mat-raised-button color="primary"
                    [disabled]="courseForm.invalid || savingCourse()"
                    (click)="saveCourse()">
                    {{ savingCourse() ? 'Saving...' : (editingCourse() ? 'Save Changes' : 'Create Course') }}
                  </button>
                </mat-card-actions>
              </mat-card>
            }

            <!-- Courses table -->
            <div class="table-wrap">
              <table mat-table [dataSource]="courses()" class="full-width">
                <ng-container matColumnDef="name">
                  <th mat-header-cell *matHeaderCellDef>Name</th>
                  <td mat-cell *matCellDef="let c">
                    <strong>{{ c.name }}</strong>
                    <div class="sub-text">{{ c.description | slice:0:80 }}{{ c.description.length > 80 ? '...' : '' }}</div>
                  </td>
                </ng-container>
                <ng-container matColumnDef="difficulty">
                  <th mat-header-cell *matHeaderCellDef>Difficulty</th>
                  <td mat-cell *matCellDef="let c">
                    <span class="diff-badge" [attr.data-diff]="c.difficulty">{{ c.difficulty }}</span>
                  </td>
                </ng-container>
                <ng-container matColumnDef="duration">
                  <th mat-header-cell *matHeaderCellDef>Hours</th>
                  <td mat-cell *matCellDef="let c">{{ c.duration_hours }}h</td>
                </ng-container>
                <ng-container matColumnDef="published">
                  <th mat-header-cell *matHeaderCellDef>Published</th>
                  <td mat-cell *matCellDef="let c">
                    <mat-icon [style.color]="c.is_published ? 'var(--success)' : 'var(--text-muted)'">
                      {{ c.is_published ? 'check_circle' : 'unpublished' }}
                    </mat-icon>
                  </td>
                </ng-container>
                <ng-container matColumnDef="tags">
                  <th mat-header-cell *matHeaderCellDef>Tags</th>
                  <td mat-cell *matCellDef="let c">
                    <span class="tag-count">{{ tagCount(c) }}</span>
                  </td>
                </ng-container>
                <ng-container matColumnDef="actions">
                  <th mat-header-cell *matHeaderCellDef></th>
                  <td mat-cell *matCellDef="let c">
                    <button mat-icon-button matTooltip="Edit" (click)="editCourse(c)"><mat-icon>edit</mat-icon></button>
                    <button mat-icon-button matTooltip="Delete" color="warn" (click)="deleteCourse(c)"><mat-icon>delete</mat-icon></button>
                  </td>
                </ng-container>
                <tr mat-header-row *matHeaderRowDef="courseColumns"></tr>
                <tr mat-row *matRowDef="let row; columns: courseColumns"></tr>
              </table>
              @if (courses().length === 0) {
                <div class="empty-state">
                  <mat-icon>school</mat-icon>
                  <p>No courses yet. Click <strong>New Course</strong> to create one.</p>
                </div>
              }
            </div>
          </div>
        </mat-tab>

        <!-- ── TAB 2: LEARNING PATHS ── -->
        <mat-tab label="Learning Paths">
          <div class="tab-content">

            @if (showPathForm()) {
              <mat-card class="form-card mb-2">
                <mat-card-header>
                  <mat-card-title>{{ editingPath() ? 'Edit Learning Path' : 'New Learning Path' }}</mat-card-title>
                </mat-card-header>
                <mat-card-content>
                  <form [formGroup]="pathForm" class="form-grid">
                    <mat-form-field appearance="outline">
                      <mat-label>Path Name</mat-label>
                      <input matInput formControlName="name" placeholder="e.g. SOC Analyst Track" />
                    </mat-form-field>
                    <mat-form-field appearance="outline" class="full-span">
                      <mat-label>Description</mat-label>
                      <textarea matInput formControlName="description" rows="2"></textarea>
                    </mat-form-field>
                    <mat-form-field appearance="outline" class="full-span">
                      <mat-label>Courses</mat-label>
                      <mat-select panelClass="tn-select-panel" formControlName="course_ids" multiple>
                        @for (c of courses(); track c.id) {
                          <mat-option [value]="c.id">{{ c.name }}</mat-option>
                        }
                      </mat-select>
                    </mat-form-field>
                  </form>
                </mat-card-content>
                <mat-card-actions align="end">
                  <button mat-button (click)="cancelPathForm()">Cancel</button>
                  <button mat-raised-button color="primary"
                    [disabled]="pathForm.invalid || savingPath()"
                    (click)="savePath()">
                    {{ savingPath() ? 'Saving...' : (editingPath() ? 'Save Changes' : 'Create Path') }}
                  </button>
                </mat-card-actions>
              </mat-card>
            }

            <div class="table-wrap">
              <table mat-table [dataSource]="learningPaths()" class="full-width">
                <ng-container matColumnDef="name">
                  <th mat-header-cell *matHeaderCellDef>Name</th>
                  <td mat-cell *matCellDef="let p"><strong>{{ p.name }}</strong></td>
                </ng-container>
                <ng-container matColumnDef="description">
                  <th mat-header-cell *matHeaderCellDef>Description</th>
                  <td mat-cell *matCellDef="let p">{{ p.description | slice:0:100 }}{{ p.description?.length > 100 ? '...' : '' }}</td>
                </ng-container>
                <ng-container matColumnDef="courses">
                  <th mat-header-cell *matHeaderCellDef>Courses</th>
                  <td mat-cell *matCellDef="let p">{{ p.course_ids?.length || 0 }} course{{ (p.course_ids?.length || 0) === 1 ? '' : 's' }}</td>
                </ng-container>
                <ng-container matColumnDef="actions">
                  <th mat-header-cell *matHeaderCellDef></th>
                  <td mat-cell *matCellDef="let p">
                    <button mat-icon-button matTooltip="Edit" (click)="editPath(p)"><mat-icon>edit</mat-icon></button>
                    <button mat-icon-button matTooltip="Delete" color="warn" (click)="deletePath(p)"><mat-icon>delete</mat-icon></button>
                  </td>
                </ng-container>
                <tr mat-header-row *matHeaderRowDef="pathColumns"></tr>
                <tr mat-row *matRowDef="let row; columns: pathColumns"></tr>
              </table>
              @if (learningPaths().length === 0) {
                <div class="empty-state">
                  <mat-icon>route</mat-icon>
                  <p>No learning paths yet.</p>
                </div>
              }
            </div>
          </div>
        </mat-tab>

        <!-- ── TAB 3: ENROLLMENTS ── -->
        <mat-tab label="Enrollments">
          <div class="tab-content">
            <div class="table-wrap">
              <table mat-table [dataSource]="enrollments()" class="full-width">
                <ng-container matColumnDef="user">
                  <th mat-header-cell *matHeaderCellDef>User</th>
                  <td mat-cell *matCellDef="let e">{{ e.user_name || e.user_id }}</td>
                </ng-container>
                <ng-container matColumnDef="course">
                  <th mat-header-cell *matHeaderCellDef>Course</th>
                  <td mat-cell *matCellDef="let e">{{ e.course_name || e.course_id }}</td>
                </ng-container>
                <ng-container matColumnDef="enrolled_at">
                  <th mat-header-cell *matHeaderCellDef>Enrolled</th>
                  <td mat-cell *matCellDef="let e">{{ e.enrolled_at | date:'mediumDate' }}</td>
                </ng-container>
                <ng-container matColumnDef="progress">
                  <th mat-header-cell *matHeaderCellDef>Progress</th>
                  <td mat-cell *matCellDef="let e">
                    <div class="progress-cell">
                      <mat-progress-bar mode="determinate" [value]="e.progress_pct || 0" color="primary"></mat-progress-bar>
                      <span class="progress-pct">{{ e.progress_pct || 0 }}%</span>
                    </div>
                  </td>
                </ng-container>
                <tr mat-header-row *matHeaderRowDef="enrollmentColumns"></tr>
                <tr mat-row *matRowDef="let row; columns: enrollmentColumns"></tr>
              </table>
              @if (enrollments().length === 0) {
                <div class="empty-state">
                  <mat-icon>people</mat-icon>
                  <p>No enrollments yet. Enrollments appear once trainees sign up for courses.</p>
                </div>
              }
            </div>
          </div>
        </mat-tab>

        <!-- ── TAB 4: INDIVIDUAL TRAINING RECORDS ── -->
        <mat-tab label="Training Records">
          <div class="tab-content">

            <mat-card class="filter-card mb-2">
              <mat-card-content>
                <div class="filter-row">
                  <mat-form-field appearance="outline" class="user-picker">
                    <mat-label>Select trainee</mat-label>
                    <mat-select panelClass="tn-select-panel" [(ngModel)]="selectedUserId" (ngModelChange)="loadTranscript($event)">
                      @for (u of users(); track u.id) {
                        <mat-option [value]="u.id">{{ u.display_name }} &mdash; {{ u.email }}</mat-option>
                      }
                    </mat-select>
                  </mat-form-field>
                </div>
              </mat-card-content>
            </mat-card>

            @if (selectedUserId && transcript()) {
              <!-- Stats row -->
              <div class="stats-row mb-2">
                <mat-card class="stat-card">
                  <mat-card-content>
                    <mat-icon>timer</mat-icon>
                    <div class="stat-value">{{ transcript()!.total_hours }}</div>
                    <div class="stat-label">Training Hours</div>
                  </mat-card-content>
                </mat-card>
                <mat-card class="stat-card">
                  <mat-card-content>
                    <mat-icon>fact_check</mat-icon>
                    <div class="stat-value">{{ transcript()!.entries.length }}</div>
                    <div class="stat-label">Activities</div>
                  </mat-card-content>
                </mat-card>
                <mat-card class="stat-card">
                  <mat-card-content>
                    <mat-icon>workspace_premium</mat-icon>
                    <div class="stat-value">{{ transcript()!.certifications.length }}</div>
                    <div class="stat-label">Certifications</div>
                  </mat-card-content>
                </mat-card>
              </div>

              <!-- Transcript table -->
              <mat-card class="mb-2">
                <mat-card-header>
                  <mat-card-title>Transcript</mat-card-title>
                </mat-card-header>
                <mat-card-content>
                  @if (transcript()!.entries.length > 0) {
                    <table mat-table [dataSource]="transcript()!.entries" class="full-width">
                      <ng-container matColumnDef="source">
                        <th mat-header-cell *matHeaderCellDef>Source</th>
                        <td mat-cell *matCellDef="let e">
                          <span class="source-badge" [attr.data-source]="e.source">{{ e.source }}</span>
                        </td>
                      </ng-container>
                      <ng-container matColumnDef="title">
                        <th mat-header-cell *matHeaderCellDef>Activity</th>
                        <td mat-cell *matCellDef="let e">{{ e.title }}</td>
                      </ng-container>
                      <ng-container matColumnDef="type">
                        <th mat-header-cell *matHeaderCellDef>Type</th>
                        <td mat-cell *matCellDef="let e">{{ e.activity_type }}</td>
                      </ng-container>
                      <ng-container matColumnDef="score">
                        <th mat-header-cell *matHeaderCellDef>Score</th>
                        <td mat-cell *matCellDef="let e">{{ e.grade || (e.score !== null ? e.score + '%' : '—') }}</td>
                      </ng-container>
                      <ng-container matColumnDef="completed">
                        <th mat-header-cell *matHeaderCellDef>Completed</th>
                        <td mat-cell *matCellDef="let e">{{ e.completed_at ? (e.completed_at | date:'mediumDate') : '—' }}</td>
                      </ng-container>
                      <tr mat-header-row *matHeaderRowDef="transcriptColumns"></tr>
                      <tr mat-row *matRowDef="let row; columns: transcriptColumns"></tr>
                    </table>
                  } @else {
                    <p class="empty-msg">No transcript entries for this user.</p>
                  }
                </mat-card-content>
              </mat-card>

              <!-- Certifications -->
              @if (transcript()!.certifications.length > 0) {
                <mat-card>
                  <mat-card-header>
                    <mat-card-title>Certifications</mat-card-title>
                  </mat-card-header>
                  <mat-card-content>
                    <div class="cert-grid">
                      @for (cert of transcript()!.certifications; track cert.credential_id) {
                        <mat-card class="cert-card">
                          <mat-card-header>
                            <mat-icon mat-card-avatar style="color:var(--accent)">verified</mat-icon>
                            <mat-card-title>{{ cert.cert_name }}</mat-card-title>
                            <mat-card-subtitle>{{ cert.issuer }}</mat-card-subtitle>
                          </mat-card-header>
                          <mat-card-content>
                            <p>Issued: {{ cert.issued_at | date:'mediumDate' }}</p>
                            @if (cert.expires_at) { <p>Expires: {{ cert.expires_at | date:'mediumDate' }}</p> }
                            <span class="status-chip" [class]="cert.status">{{ cert.status }}</span>
                          </mat-card-content>
                        </mat-card>
                      }
                    </div>
                  </mat-card-content>
                </mat-card>
              }

            } @else if (selectedUserId && !transcript()) {
              <mat-card><mat-card-content><p>Loading transcript...</p></mat-card-content></mat-card>
            } @else {
              <div class="empty-state">
                <mat-icon>person_search</mat-icon>
                <p>Select a trainee above to view their individual training record.</p>
              </div>
            }

          </div>
        </mat-tab>
      </mat-tab-group>
    </div>
  `,
  styles: [`
    .subtitle { color: var(--text-secondary); margin-bottom: 0; }
    .page-icon { font-size: 32px; width: 32px; height: 32px; color: var(--accent); margin-right: 12px; }
    .header-left { display: flex; align-items: center; }
    .header-actions { display: flex; gap: 8px; }
    .tab-content { padding: 20px 0; }
    .mb-2 { margin-bottom: 16px; }

    .form-card { margin-bottom: 20px; }
    .form-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0 16px;
    }
    .form-grid mat-form-field { width: 100%; }
    .full-span { grid-column: 1 / -1; }
    .toggle-row { display: flex; align-items: center; padding: 8px 0; }

    .full-width { width: 100%; }
    .empty-state {
      display: flex; flex-direction: column; align-items: center;
      padding: 48px 24px; color: var(--text-muted); gap: 8px; text-align: center;
    }
    .empty-state mat-icon { font-size: 48px; width: 48px; height: 48px; opacity: 0.4; }
    .empty-msg { color: var(--text-muted); margin: 16px 0; }

    .diff-badge {
      padding: 3px 10px; border-radius: 10px; font-size: 11px; font-weight: 600;
      text-transform: capitalize;
    }
    .diff-badge[data-diff="beginner"]     { background: rgba(31,182,166,0.2); color: var(--success); }
    .diff-badge[data-diff="intermediate"] { background: rgba(232,168,56,0.2); color: var(--warning); }
    .diff-badge[data-diff="advanced"]     { background: rgba(210,38,48,0.2); color: var(--alert); }
    .diff-badge[data-diff="expert"]       { background: rgba(210,38,48,0.35); color: var(--alert); }

    .tag-count { color: var(--text-muted); font-size: 13px; }
    .sub-text { color: var(--text-muted); font-size: 12px; margin-top: 2px; }

    .filter-card { background: var(--bg-secondary) !important; }
    .filter-row { display: flex; align-items: center; gap: 16px; padding: 4px 0; }
    .user-picker { min-width: 380px; }

    .stats-row { display: flex; gap: 16px; flex-wrap: wrap; }
    .stats-row mat-card { flex: 1; min-width: 140px; text-align: center; }
    .stat-value { font-size: 28px; font-weight: 700; color: var(--accent); margin: 4px 0; }
    .stat-label { font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }

    .transcriptColumns { width: 100%; }
    .source-badge {
      padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; text-transform: uppercase;
    }
    .source-badge[data-source="truenorth"] { background: var(--accent-muted); color: var(--accent); }
    .source-badge[data-source="moodle"]    { background: rgba(249,128,18,0.2); color: #f98012; }
    .source-badge[data-source="external"]  { background: rgba(99,102,241,0.2); color: #6366f1; }

    .cert-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; margin-top: 8px; }
    .cert-card { border: 1px solid var(--border); border-radius: 8px; }

    .progress-cell { display: flex; align-items: center; gap: 8px; min-width: 120px; }
    .progress-pct { font-size: 12px; color: var(--text-muted); white-space: nowrap; }
  `],
})
export class TrainingComponent implements OnInit {
  // Tab state
  activeTab = signal(0);

  // Courses
  courses = signal<Course[]>([]);
  courseColumns = ['name', 'difficulty', 'duration', 'published', 'tags', 'actions'];
  showCourseForm = signal(false);
  editingCourse = signal<Course | null>(null);
  savingCourse = signal(false);
  courseForm: FormGroup;

  // Learning paths
  learningPaths = signal<LearningPath[]>([]);
  pathColumns = ['name', 'description', 'courses', 'actions'];
  showPathForm = signal(false);
  editingPath = signal<LearningPath | null>(null);
  savingPath = signal(false);
  pathForm: FormGroup;

  // Enrollments
  enrollments = signal<Enrollment[]>([]);
  enrollmentColumns = ['user', 'course', 'enrolled_at', 'progress'];

  // Training records
  users = signal<UserSummary[]>([]);
  selectedUserId: string | null = null;
  transcript = signal<Transcript | null>(null);
  transcriptColumns = ['source', 'title', 'type', 'score', 'completed'];

  constructor(
    private api: ApiService,
    private notify: NotificationService,
    private http: HttpClient,
    private fb: FormBuilder,
  ) {
    this.courseForm = this.fb.group({
      name: ['', Validators.required],
      description: ['', Validators.required],
      difficulty: ['intermediate', Validators.required],
      duration_hours: [1, [Validators.required, Validators.min(1)]],
      tags: [''],
      is_published: [false],
    });

    this.pathForm = this.fb.group({
      name: ['', Validators.required],
      description: [''],
      course_ids: [[]],
    });
  }

  ngOnInit() {
    this.loadCourses();
    this.loadLearningPaths();
    this.loadEnrollments();
    this.loadUsers();
  }

  // ── Tag helper ──
  tagCount(course: Course): string {
    if (!course.tags) return '0 tags';
    const tags = Array.isArray(course.tags)
      ? course.tags
      : (() => { try { return JSON.parse(course.tags as string); } catch { return []; } })();
    const n = tags.length;
    return n === 0 ? 'no tags' : `${n} tag${n === 1 ? '' : 's'}`;
  }

  // ── Courses ──
  loadCourses() {
    this.api.get<any>('/courses').subscribe({
      next: res => this.courses.set(Array.isArray(res) ? res : (res.items || [])),
      error: () => this.courses.set([]),
    });
  }

  openCourseForm() {
    this.editingCourse.set(null);
    this.courseForm.reset({ difficulty: 'intermediate', duration_hours: 1, is_published: false });
    this.showCourseForm.set(true);
  }

  editCourse(c: Course) {
    this.editingCourse.set(c);
    const tagsStr = Array.isArray(c.tags) ? c.tags.join(', ')
      : (() => { try { return JSON.parse(c.tags as string).join(', '); } catch { return c.tags || ''; } })();
    this.courseForm.setValue({
      name: c.name,
      description: c.description,
      difficulty: c.difficulty,
      duration_hours: c.duration_hours,
      tags: tagsStr,
      is_published: c.is_published,
    });
    this.showCourseForm.set(true);
  }

  cancelCourseForm() {
    this.showCourseForm.set(false);
    this.editingCourse.set(null);
  }

  saveCourse() {
    if (this.courseForm.invalid) return;
    this.savingCourse.set(true);
    const v = this.courseForm.value;
    const payload = {
      ...v,
      tags: v.tags ? v.tags.split(',').map((t: string) => t.trim()).filter(Boolean) : [],
    };
    const editing = this.editingCourse();
    const req = editing
      ? this.api.patch(`/courses/${editing.id}`, payload)
      : this.api.post('/courses', payload);
    req.subscribe({
      next: () => {
        this.notify.success(editing ? 'Course updated' : 'Course created');
        this.savingCourse.set(false);
        this.cancelCourseForm();
        this.loadCourses();
      },
      error: (err: any) => {
        this.notify.error(err.error?.detail || 'Save failed');
        this.savingCourse.set(false);
      },
    });
  }

  deleteCourse(c: Course) {
    if (!confirm(`Delete course "${c.name}"? This cannot be undone.`)) return;
    this.api.delete(`/courses/${c.id}`).subscribe({
      next: () => { this.notify.success('Course deleted'); this.loadCourses(); },
      error: (err: any) => this.notify.error(err.error?.detail || 'Delete failed'),
    });
  }

  // ── Learning Paths ──
  loadLearningPaths() {
    this.api.get<LearningPath[]>('/learning-paths').subscribe({
      next: res => this.learningPaths.set(res || []),
      error: () => this.learningPaths.set([]),
    });
  }

  openPathForm() {
    this.editingPath.set(null);
    this.pathForm.reset({ course_ids: [] });
    this.showPathForm.set(true);
  }

  editPath(p: LearningPath) {
    this.editingPath.set(p);
    this.pathForm.setValue({ name: p.name, description: p.description || '', course_ids: p.course_ids || [] });
    this.showPathForm.set(true);
  }

  cancelPathForm() {
    this.showPathForm.set(false);
    this.editingPath.set(null);
  }

  savePath() {
    if (this.pathForm.invalid) return;
    this.savingPath.set(true);
    const payload = this.pathForm.value;
    const editing = this.editingPath();
    const req = editing
      ? this.api.patch(`/learning-paths/${editing.id}`, payload)
      : this.api.post('/learning-paths', payload);
    req.subscribe({
      next: () => {
        this.notify.success(editing ? 'Path updated' : 'Path created');
        this.savingPath.set(false);
        this.cancelPathForm();
        this.loadLearningPaths();
      },
      error: (err: any) => {
        this.notify.error(err.error?.detail || 'Save failed');
        this.savingPath.set(false);
      },
    });
  }

  deletePath(p: LearningPath) {
    if (!confirm(`Delete learning path "${p.name}"?`)) return;
    this.api.delete(`/learning-paths/${p.id}`).subscribe({
      next: () => { this.notify.success('Path deleted'); this.loadLearningPaths(); },
      error: (err: any) => this.notify.error(err.error?.detail || 'Delete failed'),
    });
  }

  // ── Enrollments ──
  loadEnrollments() {
    // Get all courses first, then aggregate enrollments
    this.api.get<any>('/courses').subscribe({
      next: res => {
        const courseList: Course[] = Array.isArray(res) ? res : (res.items || []);
        const courseMap = new Map(courseList.map(c => [c.id, c.name]));
        const allEnrollments: Enrollment[] = [];
        if (courseList.length === 0) { this.enrollments.set([]); return; }
        let remaining = courseList.length;
        courseList.forEach(course => {
          this.api.get<any[]>(`/courses/${course.id}/enrollments`).subscribe({
            next: (enr: any[]) => {
              (enr || []).forEach((e: any) => {
                allEnrollments.push({ ...e, course_name: courseMap.get(e.course_id) || e.course_id });
              });
            },
            error: () => {},
            complete: () => {
              remaining--;
              if (remaining === 0) this.enrollments.set(allEnrollments);
            },
          });
        });
      },
      error: () => this.enrollments.set([]),
    });
  }

  // ── Training Records ──
  loadUsers() {
    this.http.get<any>('/api/admin/users').subscribe({
      next: res => {
        const list = Array.isArray(res) ? res : (res.items || []);
        this.users.set(list.map((u: any) => ({ id: u.id, display_name: u.display_name, email: u.email })));
      },
      error: () => this.users.set([]),
    });
  }

  loadTranscript(userId: string) {
    this.transcript.set(null);
    this.api.get<Transcript>(`/users/${userId}/transcript`).subscribe({
      next: t => this.transcript.set(t),
      error: () => this.transcript.set({ entries: [], certifications: [], total_hours: 0 }),
    });
  }
}