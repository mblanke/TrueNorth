import { ChangeDetectionStrategy, Component, DestroyRef, OnInit, computed, inject, signal } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatRadioModule } from '@angular/material/radio';
import { firstValueFrom } from 'rxjs';

import { Cmi5ApiService, Cmi5Content } from '@core/services/cmi5-api.service';
import { Cmi5Au, isCmi5Launch } from './cmi5-au';

const LETTERS = 'ABCDEFGH';

const LAUNCH_KEY = (path: string) => `cmi5-launch:${path}`;

/**
 * The launch parameters carry the one-time fetch URL. Take them out of the address bar at
 * once, before any request, so they are not bookmarked, shared or sent on as a Referer; keep
 * them for this tab only, so a reload resumes the same session (whose token is kept the same
 * way, cmi5-au.ts). Returns the launch's query string, '' for none.
 */
export function takeLaunch(search: string): string {
  const path = window.location.pathname;
  let store: Storage | null = null;
  try {
    store = window.sessionStorage;
  } catch {
    store = null;
  }
  if (isCmi5Launch(search)) {
    store?.setItem(LAUNCH_KEY(path), search);
    history.replaceState(history.state, '', path + window.location.hash);
    return search;
  }
  return store?.getItem(LAUNCH_KEY(path)) ?? search;
}

/** No Referer from this page, whatever the server's header said; undone on leaving it. */
export function refuseReferrer(): () => void {
  const meta = document.createElement('meta');
  meta.name = 'referrer';
  meta.content = 'no-referrer';
  document.head.appendChild(meta);
  return () => meta.remove();
}

/** Makes the cmi5 session for a launch. A seam for the unit specs. */
export type AuFactory = (search: string) => Cmi5Au;
export const defaultAuFactory: AuFactory = (search: string) => new Cmi5Au(search);

/**
 * TrueNorth's cmi5 assignable-unit runtime: `/au/releases/:releaseId/:auIndex`.
 *
 * Launched by an LMS (TrueNorth itself, Moodle, PCTE) with the cmi5 launch parameters, it
 * runs the session in `Cmi5Au` (initialized, completed after the last page, passed or
 * failed from the server-marked quiz, terminated on Exit or when the window closes) and
 * shows the module's pages and quiz, read from TrueNorth (signed in). Opened without a
 * launch it is a preview: the same pages, nothing tracked.
 */
@Component({
  selector: 'tn-cmi5-au-runtime',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MatButtonModule, MatCardModule, MatProgressBarModule, MatRadioModule],
  template: `
    <div class="au">
      <mat-card class="panel">
        <header>
          <h1>{{ content()?.title ?? 'Module' }}</h1>
          <p class="status" aria-live="polite" data-testid="au-status">{{ status() }}</p>
        </header>
        @if (busy()) {
          <mat-progress-bar mode="indeterminate" />
        }
        @if (content(); as c) {
          @if (!quizOpen()) {
            <article [attr.lang]="c.lang" [innerHTML]="page()" data-testid="au-page"></article>
            <nav>
              <button mat-stroked-button (click)="go(index() - 1)" [disabled]="index() === 0">Previous</button>
              <span data-testid="au-position">{{ index() + 1 }} / {{ c.pages.length }}</span>
              <button mat-raised-button color="primary" (click)="next()" data-testid="au-next">
                {{ lastPage() ? 'Finish reading' : 'Next' }}
              </button>
            </nav>
          } @else if (c.quiz; as quiz) {
            <section class="quiz" data-testid="au-quiz">
              <h2>{{ quiz.title }}</h2>
              @for (q of quiz.questions; track q.id; let qi = $index) {
                <fieldset>
                  <legend>{{ qi + 1 }}. {{ q.stem }}</legend>
                  <mat-radio-group [disabled]="!!result()" (change)="answer(q.id, $event.value)">
                    @for (opt of q.options; track $index; let oi = $index) {
                      <mat-radio-button [value]="letter(oi)" [attr.data-testid]="'au-q' + qi + '-' + letter(oi)">
                        {{ letter(oi) }}) {{ opt }}
                      </mat-radio-button>
                    }
                  </mat-radio-group>
                </fieldset>
              }
              @if (result(); as r) {
                <p class="result" data-testid="au-result">{{ r }}</p>
              } @else {
                <button mat-raised-button color="primary" (click)="submit()" [disabled]="busy()" data-testid="au-submit">
                  Submit answers
                </button>
              }
            </section>
          }
        }
        @if (problem()) {
          <p class="error" data-testid="au-error">{{ problem() }}</p>
        }
        <p><button mat-stroked-button (click)="exit()" [disabled]="busy()" data-testid="au-exit">Exit</button></p>
      </mat-card>
    </div>
  `,
  styles: [`
    .au { display: flex; justify-content: center; padding: 32px 16px; }
    .panel { width: min(820px, 100%); padding: 24px; border: 1px solid var(--border); display: flex; flex-direction: column; gap: 12px; }
    h1 { margin: 0; font-size: 22px; }
    .status { margin: 4px 0 0; color: var(--text-muted); }
    article { line-height: 1.55; }
    nav { display: flex; align-items: center; gap: 12px; }
    fieldset { border: 1px solid var(--border); border-radius: 6px; margin: 12px 0; padding: 12px; }
    mat-radio-group { display: flex; flex-direction: column; gap: 4px; }
    .result { font-weight: 600; }
    .error { color: var(--severity-high); }
  `],
})
export class Cmi5AuRuntimeComponent implements OnInit {
  private readonly api = inject(Cmi5ApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  /** Overridable in specs; production makes a real session. */
  static auFactory: AuFactory = defaultAuFactory;
  /** Where Exit goes after `terminated` when the launch gave no returnURL. */
  static leave: (url: string) => void = url => window.location.assign(url);

  readonly content = signal<Cmi5Content | null>(null);
  readonly index = signal(0);
  readonly quizOpen = signal(false);
  readonly status = signal('Starting…');
  readonly problem = signal('');
  readonly busy = signal(true);
  readonly result = signal('');
  readonly page = computed(() => this.content()?.pages[this.index()]?.html ?? '');
  readonly lastPage = computed(() => this.index() >= (this.content()?.pages.length ?? 1) - 1);

  private au: Cmi5Au | null = null;
  private releaseId = '';
  private auIndex = 0;
  private answers: Record<string, string> = {};

  ngOnInit(): void {
    this.releaseId = this.route.snapshot.paramMap.get('releaseId') ?? '';
    this.auIndex = Number(this.route.snapshot.paramMap.get('auIndex') ?? '0');
    const params = this.route.snapshot.queryParamMap;
    const search = takeLaunch(
      new URLSearchParams(params.keys.map(k => [k, params.get(k) ?? ''] as [string, string])).toString(),
    );
    const restoreReferrer = refuseReferrer();
    this.destroyRef.onDestroy(restoreReferrer);
    const onLeave = () => {
      if (this.au && !this.au.terminated) void this.au.terminate({ keepalive: true }).catch(() => undefined);
    };
    window.addEventListener('pagehide', onLeave);
    this.destroyRef.onDestroy(() => window.removeEventListener('pagehide', onLeave));
    void this.begin(search);
  }

  private async begin(search: string): Promise<void> {
    try {
      if (isCmi5Launch(search)) {
        this.au = Cmi5AuRuntimeComponent.auFactory(search);
        await this.au.start();
        const ms = this.au.masteryScore;
        this.status.set(`Tracked session (${this.au.mode}).${ms === null ? '' : ` Pass mark: ${Math.round(ms * 100)}%.`}`);
      } else {
        this.status.set('Preview: not tracked. Launch this module from your course to record your progress.');
      }
      this.content.set(await firstValueFrom(this.api.content(this.releaseId, this.auIndex)));
      if (!this.content()!.pages.length) {
        await this.finishReading();
      } else {
        await this.progressed();
      }
    } catch (e) {
      this.fail(e);
    } finally {
      this.busy.set(false);
    }
  }

  letter(i: number): string {
    return LETTERS[i] ?? String(i);
  }

  async go(i: number): Promise<void> {
    const pages = this.content()?.pages.length ?? 0;
    this.index.set(Math.max(0, Math.min(pages - 1, i)));
    window.scrollTo?.(0, 0);
    await this.progressed();
  }

  async next(): Promise<void> {
    if (this.lastPage()) {
      await this.finishReading();
    } else {
      await this.go(this.index() + 1);
    }
  }

  answer(questionId: string, letter: string): void {
    this.answers[questionId] = letter;
  }

  /** Marked by TrueNorth; the AU reports the score to its LMS against the masteryScore
   *  (the module's own pass mark when the LMS sets none). */
  async submit(): Promise<void> {
    const c = this.content();
    if (!c?.quiz || this.result()) return;
    this.busy.set(true);
    try {
      const g = await firstValueFrom(this.api.grade(this.releaseId, this.auIndex, this.answers));
      const fallback = c.mastery_score ?? 0.5;
      const pass = this.au?.masteryScore ?? fallback;
      const ok = g.scaled >= pass;
      this.result.set(`${g.correct} / ${g.total} correct (${Math.round(g.scaled * 100)}%). ${ok ? 'Passed' : 'Not passed'}.`);
      if (this.au?.judged) {
        await this.au.score(g.scaled, { raw: g.correct, min: 0, max: g.total }, fallback);
      }
      this.status.set(this.au ? 'Quiz submitted. Use Exit to return to your course.' : 'Quiz submitted (preview).');
    } catch (e) {
      this.fail(e);
    } finally {
      this.busy.set(false);
    }
  }

  /** `terminated`, then back to the LMS (returnURL), or to the course in TrueNorth. */
  async exit(): Promise<void> {
    this.busy.set(true);
    try {
      if (this.au && !this.au.terminated) {
        try {
          window.sessionStorage.removeItem(LAUNCH_KEY(window.location.pathname));
        } catch {
          /* storage may be unavailable */
        }
        await this.au.terminate({ redirect: url => Cmi5AuRuntimeComponent.leave(url) });
        if (this.au.launchData.returnURL) return;
      }
      void this.router.navigate(['/au/releases', this.releaseId]);
    } catch (e) {
      this.fail(e);
    } finally {
      this.busy.set(false);
    }
  }

  private async finishReading(): Promise<void> {
    if (this.au?.judged) {
      await this.au.complete();
    }
    if (this.content()?.quiz) {
      this.quizOpen.set(true);
    } else {
      this.status.set(this.au ? 'Module complete. Use Exit to return to your course.' : 'Module complete (preview).');
    }
  }

  private async progressed(): Promise<void> {
    const pages = this.content()?.pages.length ?? 0;
    if (this.au && pages) {
      await this.au.progress((100 * (this.index() + 1)) / pages);
    }
  }

  private fail(e: unknown): void {
    const detail = (e as { error?: { detail?: string } })?.error?.detail;
    this.problem.set(detail ?? (e instanceof Error ? e.message : 'Something went wrong.'));
  }
}

