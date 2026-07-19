import { Component, OnDestroy, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterModule } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatSelectModule } from '@angular/material/select';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MatTooltipModule } from '@angular/material/tooltip';
import { Subscription, interval } from 'rxjs';

import { ApiService } from '@core/services/api.service';
import { EnterStaggerDirective, HoverLiftDirective } from '../../shared/motion';

@Component({
  selector: 'tn-curriculum-forge',
  standalone: true,
  imports: [
    CommonModule, FormsModule, RouterModule, MatButtonModule, MatCardModule,
    MatFormFieldModule, MatIconModule, MatInputModule, MatProgressBarModule,
    MatSelectModule, MatSnackBarModule, MatTooltipModule,
    EnterStaggerDirective, HoverLiftDirective,
  ],
  template: `
    <div class="page-container">
      <div class="page-header">
        <div class="header-left">
          <div>
            <div class="tn-kicker">AI Training Pipeline</div>
            <h1>Curriculum</h1>
            <p class="subtitle">Ingest courseware → generate courses, quizzes, and capability-measuring ranges</p>
          </div>
        </div>
        <div class="header-actions">
          <mat-form-field appearance="outline" subscriptSizing="dynamic">
            <mat-label>New curriculum name</mat-label>
            <input matInput [(ngModel)]="newName" placeholder="e.g. SOC Analyst Level 1">
          </mat-form-field>
          <button mat-raised-button color="primary" (click)="create()" [disabled]="!newName.trim()">
            <mat-icon>add</mat-icon> Create
          </button>
        </div>
      </div>

      @if (curricula().length === 0) {
        <mat-card class="empty-state">
          <mat-icon class="empty-big">auto_stories</mat-icon>
          <p>No curricula yet. Create one, then upload PDFs, slide decks, or docs to power AI generation.</p>
        </mat-card>
      }

      <div class="curricula-grid" tnEnterStagger>
        @for (c of curricula(); track c.id) {
          <mat-card class="curriculum-card tn-stagger-item" [class.selected]="selected()?.id === c.id"
                    tnHoverLift (click)="select(c)">
            <div class="card-top">
              <span class="curr-name">{{ c.name }}</span>
              <span class="status-chip" [class]="statusClass(c.status)">{{ c.status }}</span>
            </div>
            <div class="curr-meta">
              {{ c.documents.length || 0 }} sources · {{ c.chunk_count }} chunks
              @if (c.embedding_model) { · {{ c.embedding_model }} }
            </div>
          </mat-card>
        }
      </div>

      @if (selected(); as cur) {
        <div class="detail-grid">
          <!-- Sources panel -->
          <mat-card class="panel">
            <h2 class="panel-title"><mat-icon>upload_file</mat-icon> Sources</h2>
            <input type="file" #fileInput multiple hidden
                   accept=".pdf,.docx,.pptx,.md,.txt,.html"
                   (change)="upload(fileInput.files)">
            <div class="source-actions">
              <button mat-stroked-button (click)="fileInput.click()" [disabled]="uploading()">
                <mat-icon>attach_file</mat-icon> Upload files
              </button>
              <mat-form-field appearance="outline" subscriptSizing="dynamic" class="url-field">
                <mat-label>Add page URL</mat-label>
                <input matInput [(ngModel)]="newUrl" placeholder="https://...">
              </mat-form-field>
              <button mat-stroked-button (click)="addUrl()" [disabled]="!newUrl.startsWith('http')">
                <mat-icon>link</mat-icon> Add
              </button>
            </div>
            @if (cur.status === 'ingesting') {
              <mat-progress-bar mode="indeterminate"></mat-progress-bar>
            }
            <div class="doc-list">
              @for (d of cur.documents; track d.id) {
                <div class="doc-row">
                  <mat-icon class="doc-icon">{{ d.source_url ? 'public' : 'description' }}</mat-icon>
                  <span class="doc-name" [matTooltip]="d.error || d.filename">{{ d.filename }}</span>
                  <span class="doc-chunks">{{ d.chunk_count }} chunks</span>
                  <span class="status-chip" [class]="statusClass(d.status)">{{ d.status }}</span>
                </div>
              }
              @empty { <p class="muted">No sources uploaded yet.</p> }
            </div>
          </mat-card>

          <!-- RAG search preview -->
          <mat-card class="panel">
            <h2 class="panel-title"><mat-icon>manage_search</mat-icon> Knowledge check</h2>
            <div class="search-row">
              <mat-form-field appearance="outline" subscriptSizing="dynamic" class="grow">
                <mat-label>Ask the curriculum</mat-label>
                <input matInput [(ngModel)]="ragQuery" (keyup.enter)="ragSearch()"
                       placeholder="e.g. What are the phases of incident response?">
              </mat-form-field>
              <button mat-stroked-button (click)="ragSearch()" [disabled]="cur.status !== 'ready'">
                <mat-icon>search</mat-icon>
              </button>
            </div>
            @for (hit of ragHits(); track $index) {
              <div class="rag-hit">
                <div class="rag-source">{{ hit.filename }} · chunk {{ hit.chunk_ordinal }}</div>
                <div class="rag-text">{{ hit.text | slice:0:420 }}…</div>
              </div>
            }
          </mat-card>

          <!-- Generators -->
          <mat-card class="panel">
            <h2 class="panel-title"><mat-icon>auto_awesome</mat-icon> Generate course</h2>
            <div class="gen-row">
              <mat-form-field appearance="outline" subscriptSizing="dynamic">
                <mat-label>Difficulty</mat-label>
                <mat-select panelClass="tn-select-panel" [(ngModel)]="courseDifficulty">
                  <mat-option value="beginner">Beginner</mat-option>
                  <mat-option value="intermediate">Intermediate</mat-option>
                  <mat-option value="advanced">Advanced</mat-option>
                  <mat-option value="expert">Expert</mat-option>
                </mat-select>
              </mat-form-field>
              <mat-form-field appearance="outline" subscriptSizing="dynamic">
                <mat-label>Modules</mat-label>
                <input matInput type="number" min="2" max="16" [(ngModel)]="courseModules">
              </mat-form-field>
            </div>
            <mat-form-field appearance="outline" subscriptSizing="dynamic" class="full-width">
              <mat-label>Focus (optional)</mat-label>
              <input matInput [(ngModel)]="courseFocus" placeholder="e.g. emphasize detection engineering">
            </mat-form-field>
            <button mat-flat-button color="primary" class="full-width"
                    (click)="generateCourse()" [disabled]="cur.status !== 'ready' || generatingCourse()">
              <mat-icon>{{ generatingCourse() ? 'hourglass_top' : 'school' }}</mat-icon>
              {{ generatingCourse() ? 'Drafting course…' : 'Draft course with AI' }}
            </button>
            @if (lastCourse(); as lc) {
              <div class="gen-result">
                ✓ Drafted “{{ lc.name }}” — {{ lc.module_count }} modules ({{ lc.quiz_module_count }} quizzes)
                <a routerLink="/learning/courses">Review in LMS</a>
              </div>
            }

            <h2 class="panel-title mt"><mat-icon>quiz</mat-icon> Generate quiz</h2>
            <mat-form-field appearance="outline" subscriptSizing="dynamic" class="full-width">
              <mat-label>Topic</mat-label>
              <input matInput [(ngModel)]="quizTopic" placeholder="e.g. Phishing triage fundamentals">
            </mat-form-field>
            <div class="gen-row">
              <mat-form-field appearance="outline" subscriptSizing="dynamic">
                <mat-label>Questions</mat-label>
                <input matInput type="number" min="3" max="30" [(ngModel)]="quizCount">
              </mat-form-field>
              <mat-form-field appearance="outline" subscriptSizing="dynamic">
                <mat-label>Difficulty</mat-label>
                <mat-select panelClass="tn-select-panel" [(ngModel)]="quizDifficulty">
                  <mat-option value="beginner">Beginner</mat-option>
                  <mat-option value="intermediate">Intermediate</mat-option>
                  <mat-option value="advanced">Advanced</mat-option>
                </mat-select>
              </mat-form-field>
            </div>
            <button mat-flat-button color="primary" class="full-width"
                    (click)="generateQuiz()" [disabled]="cur.status !== 'ready' || !quizTopic.trim() || generatingQuiz()">
              <mat-icon>{{ generatingQuiz() ? 'hourglass_top' : 'auto_fix_high' }}</mat-icon>
              {{ generatingQuiz() ? 'Writing questions…' : 'Generate quiz with AI' }}
            </button>

            <h2 class="panel-title mt"><mat-icon>radar</mat-icon> Generate range</h2>
            <p class="muted small">Turn learning objectives into a deployable exercise that measures them.</p>
            <a mat-stroked-button class="full-width" [routerLink]="['/authoring/forge']"
               [queryParams]="{ curriculum: cur.id }">
              <mat-icon>architecture</mat-icon> Open Exercise Forge (curriculum mode)
            </a>
          </mat-card>
        </div>

        <!-- Quizzes for this curriculum -->
        <h2 class="section-heading"><mat-icon>quiz</mat-icon> Quizzes</h2>
        <div class="quiz-grid">
          @for (q of quizzes(); track q.id) {
            <mat-card class="quiz-card" tnHoverLift>
              <div class="card-top">
                <span class="curr-name">{{ q.title }}</span>
                <span class="status-chip" [class]="q.is_published ? 'ready' : 'draft'">
                  {{ q.is_published ? 'published' : 'draft' }}
                </span>
              </div>
              <div class="curr-meta">{{ q.question_count }} questions · pass {{ q.pass_pct }}%
                @if (q.generated_by_model) { · {{ q.generated_by_model }} }
              </div>
              <div class="quiz-actions">
                @if (!q.is_published) {
                  <button mat-button color="primary" (click)="publish(q)">
                    <mat-icon>publish</mat-icon> Publish
                  </button>
                } @else {
                  <a mat-button color="primary" [routerLink]="['/quiz-player']" [queryParams]="{ quiz: q.id }">
                    <mat-icon>play_arrow</mat-icon> Take
                  </a>
                }
                <a mat-button [href]="exportUrl(q.id, 'gift')" target="_blank">
                  <mat-icon>download</mat-icon> GIFT
                </a>
                <a mat-button [href]="exportUrl(q.id, 'moodlexml')" target="_blank">
                  <mat-icon>download</mat-icon> Moodle XML
                </a>
              </div>
            </mat-card>
          }
          @empty { <p class="muted">No quizzes for this curriculum yet — generate one above.</p> }
        </div>
      }
    </div>
  `,
  styles: [`
    .subtitle { color: var(--text-muted); }
    .header-actions { display: flex; gap: 12px; align-items: center; }
    .empty-state { text-align: center; padding: 48px; color: var(--text-muted); }
    .empty-big { font-size: 48px; width: 48px; height: 48px; color: var(--border-light); }

    .curricula-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px; }
    .curriculum-card { padding: 16px; cursor: pointer; }
    .curriculum-card.selected { border-color: var(--accent) !important; box-shadow: var(--glow-accent) !important; }
    .card-top { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
    .curr-name { font-weight: 600; color: var(--text-primary); }
    .curr-meta { font-size: 12px; color: var(--text-muted); margin-top: 6px; }

    .detail-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; margin-top: 22px; }
    @media (max-width: 1200px) { .detail-grid { grid-template-columns: 1fr; } }
    .panel { padding: 18px; }
    .panel-title {
      display: flex; align-items: center; gap: 8px;
      font-family: var(--font-display); font-size: 15px; font-weight: 700;
      color: var(--text-primary); margin: 0 0 12px;
    }
    .panel-title mat-icon { color: var(--accent); font-size: 20px; width: 20px; height: 20px; }
    .panel-title.mt { margin-top: 22px; }

    .source-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
    .url-field { flex: 1; min-width: 160px; }
    .doc-list { margin-top: 10px; display: flex; flex-direction: column; gap: 6px; max-height: 320px; overflow-y: auto; }
    .doc-row { display: flex; align-items: center; gap: 8px; font-size: 13px; }
    .doc-icon { font-size: 18px; width: 18px; height: 18px; color: var(--text-muted); }
    .doc-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-secondary); }
    .doc-chunks { font-size: 11px; color: var(--text-muted); }

    .search-row { display: flex; gap: 8px; align-items: center; }
    .grow { flex: 1; }
    .rag-hit { padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); margin-top: 8px; }
    .rag-source { font-size: 10px; letter-spacing: 1px; text-transform: uppercase; color: var(--accent); }
    .rag-text { font-size: 13px; color: var(--text-secondary); margin-top: 4px; }

    .gen-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .full-width { width: 100%; }
    .gen-result { margin-top: 10px; font-size: 13px; color: var(--success); }
    .gen-result a { margin-left: 8px; }

    .section-heading {
      display: flex; align-items: center; gap: 8px; margin: 28px 0 14px;
      font-family: var(--font-display); font-size: 18px; font-weight: 700; color: var(--text-primary);
    }
    .section-heading mat-icon { color: var(--accent); }
    .quiz-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
    .quiz-card { padding: 16px; }
    .quiz-actions { display: flex; gap: 4px; margin-top: 10px; flex-wrap: wrap; }

    .muted { color: var(--text-muted); }
    .small { font-size: 12px; }
  `],
})
export class CurriculumForgeComponent implements OnInit, OnDestroy {
  curricula = signal<any[]>([]);
  selected = signal<any | null>(null);
  quizzes = signal<any[]>([]);
  ragHits = signal<any[]>([]);
  uploading = signal(false);
  generatingCourse = signal(false);
  generatingQuiz = signal(false);
  lastCourse = signal<any | null>(null);

  newName = '';
  newUrl = '';
  ragQuery = '';
  courseDifficulty = 'intermediate';
  courseModules = 6;
  courseFocus = '';
  quizTopic = '';
  quizCount = 10;
  quizDifficulty = 'intermediate';

  private pollSub?: Subscription;

  constructor(private api: ApiService, private snack: MatSnackBar) {}

  ngOnInit(): void {
    this.refresh();
    // Light polling keeps ingestion status fresh while documents process.
    this.pollSub = interval(5000).subscribe(() => {
      const cur = this.selected();
      if (cur && (cur.status === 'ingesting' || cur.documents?.some((d: any) =>
          ['pending', 'extracting', 'embedding'].includes(d.status)))) {
        this.api.getCurriculum(cur.id).subscribe({
          next: fresh => {
            this.selected.set(fresh);
            this.curricula.update(list => list.map(c => (c.id === fresh.id ? fresh : c)));
          },
          error: () => {},
        });
      }
    });
  }

  ngOnDestroy(): void {
    this.pollSub?.unsubscribe();
  }

  refresh(): void {
    this.api.listCurricula().subscribe({
      next: list => {
        this.curricula.set(list);
        const sel = this.selected();
        if (sel) {
          const fresh = list.find(c => c.id === sel.id);
          if (fresh) this.selected.set(fresh);
        }
      },
      error: () => {},
    });
  }

  create(): void {
    this.api.createCurriculum({ name: this.newName.trim() }).subscribe({
      next: c => {
        this.newName = '';
        this.refresh();
        this.select(c);
        this.snack.open('Curriculum created — add sources', '', { duration: 2500 });
      },
      error: () => this.snack.open('Create failed', 'OK', { duration: 3000 }),
    });
  }

  select(c: any): void {
    this.selected.set(c);
    this.ragHits.set([]);
    this.lastCourse.set(null);
    this.loadQuizzes(c.id);
  }

  loadQuizzes(curriculumId: string): void {
    this.api.listQuizzes(curriculumId).subscribe({
      next: q => this.quizzes.set(q),
      error: () => this.quizzes.set([]),
    });
  }

  upload(files: FileList | null): void {
    const cur = this.selected();
    if (!cur || !files?.length) return;
    this.uploading.set(true);
    this.api.uploadCurriculumDocuments(cur.id, Array.from(files)).subscribe({
      next: fresh => {
        this.uploading.set(false);
        this.selected.set(fresh);
        this.snack.open('Upload accepted — ingesting in background', '', { duration: 2500 });
      },
      error: err => {
        this.uploading.set(false);
        this.snack.open(err.error?.detail || 'Upload failed', 'OK', { duration: 4000 });
      },
    });
  }

  addUrl(): void {
    const cur = this.selected();
    if (!cur) return;
    this.api.addCurriculumUrls(cur.id, [this.newUrl.trim()]).subscribe({
      next: fresh => {
        this.newUrl = '';
        this.selected.set(fresh);
      },
      error: err => this.snack.open(err.error?.detail || 'URL add failed', 'OK', { duration: 4000 }),
    });
  }

  ragSearch(): void {
    const cur = this.selected();
    if (!cur || !this.ragQuery.trim()) return;
    this.api.searchCurriculum(cur.id, this.ragQuery.trim(), 4).subscribe({
      next: hits => this.ragHits.set(hits),
      error: () => this.ragHits.set([]),
    });
  }

  generateCourse(): void {
    const cur = this.selected();
    if (!cur) return;
    this.generatingCourse.set(true);
    this.api.generateCourseFromCurriculum(cur.id, {
      difficulty: this.courseDifficulty,
      module_count: this.courseModules,
      focus: this.courseFocus,
    }).subscribe({
      next: result => {
        this.generatingCourse.set(false);
        this.lastCourse.set(result);
        this.loadQuizzes(cur.id);
        this.snack.open(`Course drafted: ${result.name}`, '', { duration: 4000, panelClass: 'snack-success' });
      },
      error: err => {
        this.generatingCourse.set(false);
        this.snack.open(err.error?.detail || 'Course generation failed', 'OK', { duration: 5000 });
      },
    });
  }

  generateQuiz(): void {
    const cur = this.selected();
    if (!cur) return;
    this.generatingQuiz.set(true);
    this.api.generateQuiz({
      curriculum_id: cur.id,
      topic: this.quizTopic.trim(),
      question_count: this.quizCount,
      difficulty: this.quizDifficulty,
    }).subscribe({
      next: quiz => {
        this.generatingQuiz.set(false);
        this.loadQuizzes(cur.id);
        this.snack.open(`Quiz drafted: ${quiz.title}`, '', { duration: 4000, panelClass: 'snack-success' });
      },
      error: err => {
        this.generatingQuiz.set(false);
        this.snack.open(err.error?.detail || 'Quiz generation failed', 'OK', { duration: 5000 });
      },
    });
  }

  publish(quiz: any): void {
    this.api.updateQuiz(quiz.id, { is_published: true }).subscribe({
      next: () => this.loadQuizzes(this.selected()!.id),
      error: () => this.snack.open('Publish failed', 'OK', { duration: 3000 }),
    });
  }

  exportUrl(quizId: string, format: 'gift' | 'moodlexml'): string {
    return this.api.quizExportUrl(quizId, format);
  }

  statusClass(status: string): string {
    return {
      ready: 'ready', indexed: 'ready', draft: 'draft', pending: 'draft',
      ingesting: 'provisioning', extracting: 'provisioning', embedding: 'provisioning',
      error: 'failed',
    }[status] || 'draft';
  }
}
