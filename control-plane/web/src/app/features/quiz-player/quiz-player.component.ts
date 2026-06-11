import { Component, OnDestroy, OnInit, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, RouterModule } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import gsap from 'gsap';

import { ApiService } from '@core/services/api.service';
import { MotionService } from '../../shared/motion';
import { LottieIconComponent } from '../../shared/components/lottie-icon.component';

@Component({
  selector: 'tn-quiz-player',
  standalone: true,
  imports: [
    CommonModule, RouterModule, MatButtonModule, MatCardModule,
    MatIconModule, MatProgressBarModule, MatSnackBarModule,
    LottieIconComponent,
  ],
  template: `
    <div class="player-shell">
      @if (!attempt() && !result()) {
        <mat-card class="intro-card">
          <mat-icon class="big-icon">quiz</mat-icon>
          <h1>{{ quiz()?.title || 'Quiz' }}</h1>
          <p class="muted">{{ quiz()?.description }}</p>
          <div class="intro-meta">
            <span><mat-icon>format_list_numbered</mat-icon> {{ quiz()?.question_count }} questions</span>
            <span><mat-icon>flag</mat-icon> pass {{ quiz()?.pass_pct }}%</span>
            @if (quiz()?.time_limit_minutes) {
              <span><mat-icon>timer</mat-icon> {{ quiz()?.time_limit_minutes }} min</span>
            }
          </div>
          <button mat-raised-button color="primary" class="start-btn" (click)="start()" [disabled]="starting()">
            <mat-icon>play_arrow</mat-icon> Start attempt
          </button>
        </mat-card>
      }

      @if (attempt(); as att) {
        <div class="hud">
          <span class="hud-title">{{ att.title }}</span>
          <span class="spacer"></span>
          @if (remainingSeconds() !== null) {
            <span class="timer" [class.low]="remainingSeconds()! < 60">
              <mat-icon>timer</mat-icon> {{ timerLabel() }}
            </span>
          }
          <span class="progress-label">{{ index() + 1 }} / {{ att.questions.length }}</span>
        </div>
        <mat-progress-bar mode="determinate" [value]="(index() + 1) / att.questions.length * 100"></mat-progress-bar>

        @if (current(); as q) {
          <mat-card class="question-card" #qcard>
            <div class="q-type">{{ typeLabel(q.question_type) }} · {{ q.points }} pts</div>
            <div class="q-stem">{{ q.stem }}</div>

            @if (q.question_type === 'multi') {
              @for (opt of q.options; track $index) {
                <button type="button" class="option" [class.checked]="isSelected(q.id, $index)"
                        role="checkbox" [attr.aria-checked]="isSelected(q.id, $index)"
                        (click)="toggleMulti(q.id, $index)">
                  <span class="option-box" [class.on]="isSelected(q.id, $index)">
                    @if (isSelected(q.id, $index)) { <mat-icon>check</mat-icon> }
                  </span>
                  {{ opt }}
                </button>
              }
            } @else {
              @for (opt of q.options; track $index) {
                <button type="button" class="option" [class.checked]="isSelected(q.id, $index)"
                        role="radio" [attr.aria-checked]="isSelected(q.id, $index)"
                        (click)="selectSingle(q.id, $index)">
                  <span class="option-dot" [class.on]="isSelected(q.id, $index)"></span>
                  {{ opt }}
                </button>
              }
            }

            <div class="nav-row">
              <button mat-stroked-button (click)="prev()" [disabled]="index() === 0">
                <mat-icon>arrow_back</mat-icon> Back
              </button>
              @if (index() < att.questions.length - 1) {
                <button mat-flat-button color="primary" (click)="next()">
                  Next <mat-icon>arrow_forward</mat-icon>
                </button>
              } @else {
                <button mat-raised-button color="primary" (click)="submit()" [disabled]="submitting()">
                  <mat-icon>done_all</mat-icon> Submit ({{ answeredCount() }}/{{ att.questions.length }} answered)
                </button>
              }
            </div>
          </mat-card>
        }
      }

      @if (result(); as res) {
        <mat-card class="result-card">
          @if (res.passed) { <tn-lottie name="star-pop" [size]="110" /> }
          @else { <mat-icon class="big-icon fail">sentiment_dissatisfied</mat-icon> }
          <h1 [class.pass]="res.passed" [class.fail-text]="!res.passed">
            {{ res.passed ? 'Passed!' : 'Not yet' }}
          </h1>
          <div class="score-line">
            <span class="score tn-display tn-gradient-text">{{ res.pct }}%</span>
            <span class="muted">{{ res.score }} / {{ res.max_score }} points</span>
          </div>

          <div class="review">
            @for (r of res.results; track r.question_id) {
              <div class="review-row" [class.ok]="r.correct" [class.bad]="!r.correct">
                <mat-icon>{{ r.correct ? 'check_circle' : 'cancel' }}</mat-icon>
                <div class="review-body">
                  <div class="review-points">{{ r.points_earned }}/{{ r.points_possible }} pts</div>
                  @if (r.explanation) { <div class="review-expl">{{ r.explanation }}</div> }
                </div>
              </div>
            }
          </div>

          <div class="result-actions">
            <button mat-stroked-button (click)="retake()">
              <mat-icon>replay</mat-icon> Retake
            </button>
            <a mat-flat-button color="primary" routerLink="/my-progress">
              <mat-icon>trending_up</mat-icon> View my progress
            </a>
          </div>
        </mat-card>
      }
    </div>
  `,
  styles: [`
    .player-shell { max-width: 760px; margin: 0 auto; padding: 28px 16px 60px; }
    .muted { color: var(--text-muted); }
    .spacer { flex: 1; }

    .intro-card, .result-card { padding: 44px; text-align: center; }
    .big-icon { font-size: 56px; width: 56px; height: 56px; color: var(--accent); }
    .big-icon.fail { color: var(--warning); }
    .intro-meta { display: flex; gap: 22px; justify-content: center; margin: 18px 0 26px; color: var(--text-secondary); }
    .intro-meta span { display: flex; align-items: center; gap: 6px; font-size: 14px; }
    .intro-meta mat-icon { font-size: 18px; width: 18px; height: 18px; color: var(--accent); }
    .start-btn { height: 46px; padding: 0 32px; font-size: 15px; box-shadow: var(--glow-accent) !important; }

    .hud { display: flex; align-items: center; gap: 14px; margin-bottom: 8px; }
    .hud-title { font-family: var(--font-display); font-weight: 700; color: var(--text-primary); }
    .timer { display: flex; align-items: center; gap: 4px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
    .timer.low { color: var(--alert); }
    .timer mat-icon { font-size: 18px; width: 18px; height: 18px; }
    .progress-label { font-size: 13px; color: var(--text-muted); }

    .question-card { padding: 26px; margin-top: 14px; }
    .q-type { font-size: 10px; letter-spacing: 1.6px; text-transform: uppercase; color: var(--accent); }
    .q-stem { font-size: 17px; line-height: 1.55; color: var(--text-primary); margin: 10px 0 20px; }

    .option {
      display: flex; align-items: center; gap: 10px; width: 100%; text-align: left;
      padding: 12px 16px; margin: 8px 0;
      background: transparent; font: inherit;
      border: 1px solid var(--border); border-radius: var(--radius-sm);
      cursor: pointer; transition: border-color 0.15s, background 0.15s;
      color: var(--text-secondary);
    }
    .option-box {
      width: 18px; height: 18px; border-radius: 4px; flex-shrink: 0;
      border: 2px solid var(--border-light); display: grid; place-items: center;
      transition: all 0.15s;
    }
    .option-box.on { border-color: var(--accent); background: var(--accent); }
    .option-box mat-icon { font-size: 14px; width: 14px; height: 14px; color: var(--text-on-accent); }
    .option:hover { border-color: var(--border-light); }
    .option.checked { border-color: var(--accent); background: var(--accent-muted); color: var(--text-primary); }
    .option-dot {
      width: 14px; height: 14px; border-radius: 50%; flex-shrink: 0;
      border: 2px solid var(--border-light); transition: all 0.15s;
    }
    .option-dot.on { border-color: var(--accent); background: var(--accent); box-shadow: 0 0 8px var(--accent-muted); }

    .nav-row { display: flex; justify-content: space-between; margin-top: 22px; }

    .score-line { display: flex; flex-direction: column; gap: 4px; margin: 12px 0 22px; }
    .score { font-size: 52px; }
    h1.pass { color: var(--success); }
    h1.fail-text { color: var(--warning); }
    .review { text-align: left; max-height: 320px; overflow-y: auto; margin-bottom: 20px; }
    .review-row { display: flex; gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--border); }
    .review-row.ok mat-icon { color: var(--success); }
    .review-row.bad mat-icon { color: var(--alert); }
    .review-points { font-size: 12px; color: var(--text-muted); }
    .review-expl { font-size: 13px; color: var(--text-secondary); margin-top: 2px; }
    .result-actions { display: flex; gap: 12px; justify-content: center; }
  `],
})
export class QuizPlayerComponent implements OnInit, OnDestroy {
  quiz = signal<any | null>(null);
  attempt = signal<any | null>(null);
  result = signal<any | null>(null);
  index = signal(0);
  starting = signal(false);
  submitting = signal(false);
  remainingSeconds = signal<number | null>(null);

  current = computed(() => this.attempt()?.questions?.[this.index()] ?? null);
  answers: Record<string, number[]> = {};

  private quizId = '';
  private timerHandle: any = null;

  constructor(
    private api: ApiService,
    private route: ActivatedRoute,
    private motion: MotionService,
    private snack: MatSnackBar,
  ) {}

  ngOnInit(): void {
    this.quizId = this.route.snapshot.queryParamMap.get('quiz') ?? '';
    if (this.quizId) {
      this.api.getQuiz(this.quizId).subscribe({
        next: q => this.quiz.set(q),
        error: () => this.snack.open('Quiz not found', 'OK', { duration: 4000 }),
      });
    }
  }

  ngOnDestroy(): void {
    if (this.timerHandle) clearInterval(this.timerHandle);
  }

  start(): void {
    this.starting.set(true);
    this.api.startQuizAttempt(this.quizId).subscribe({
      next: att => {
        this.starting.set(false);
        this.answers = {};
        this.index.set(0);
        this.result.set(null);
        this.attempt.set(att);
        if (att.time_limit_minutes) {
          this.remainingSeconds.set(att.time_limit_minutes * 60);
          this.timerHandle = setInterval(() => {
            const left = (this.remainingSeconds() ?? 0) - 1;
            this.remainingSeconds.set(left);
            if (left <= 0) {
              clearInterval(this.timerHandle);
              this.snack.open('Time is up — submitting', '', { duration: 3000 });
              this.submit();
            }
          }, 1000);
        }
        this.animateIn();
      },
      error: err => {
        this.starting.set(false);
        this.snack.open(err.error?.detail || 'Could not start attempt', 'OK', { duration: 4000 });
      },
    });
  }

  timerLabel(): string {
    const s = this.remainingSeconds() ?? 0;
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }

  typeLabel(type: string): string {
    return { mcq: 'Single choice', multi: 'Multiple answers', truefalse: 'True / False', scenario: 'Scenario' }[type] || type;
  }

  isSelected(qid: string, idx: number): boolean {
    return (this.answers[qid] ?? []).includes(idx);
  }

  selectSingle(qid: string, idx: number): void {
    this.answers[qid] = [idx];
  }

  toggleMulti(qid: string, idx: number): void {
    const cur = new Set(this.answers[qid] ?? []);
    cur.has(idx) ? cur.delete(idx) : cur.add(idx);
    this.answers[qid] = [...cur].sort();
  }

  answeredCount(): number {
    return Object.values(this.answers).filter(a => a.length).length;
  }

  next(): void {
    this.index.update(i => i + 1);
    this.animateIn();
  }

  prev(): void {
    this.index.update(i => Math.max(0, i - 1));
    this.animateIn();
  }

  private animateIn(): void {
    if (this.motion.reducedMotion()) return;
    this.motion.runOutside(() => {
      requestAnimationFrame(() => {
        const card = document.querySelector('.question-card');
        if (card) {
          gsap.fromTo(card, { autoAlpha: 0, x: 24 }, { autoAlpha: 1, x: 0, duration: 0.3, ease: 'power2.out', clearProps: 'all' });
        }
      });
    });
  }

  submit(): void {
    const att = this.attempt();
    if (!att || this.submitting()) return;
    this.submitting.set(true);
    if (this.timerHandle) clearInterval(this.timerHandle);
    this.api.submitQuizAttempt(att.attempt_id, this.answers).subscribe({
      next: res => {
        this.submitting.set(false);
        this.attempt.set(null);
        this.result.set(res);
      },
      error: err => {
        this.submitting.set(false);
        this.snack.open(err.error?.detail || 'Submit failed', 'OK', { duration: 4000 });
      },
    });
  }

  retake(): void {
    this.result.set(null);
    this.start();
  }
}
