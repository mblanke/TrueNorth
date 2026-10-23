import { Component, ElementRef, OnInit, ViewChild, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { AuthService } from '@core/services/auth.service';
import { AcknowledgementApiService, AcknowledgementCurrent } from '@core/services/acknowledgement-api.service';
import { safeReturnUrl } from '@core/auth/return-url';
import { MarkdownViewComponent } from '../../shared/markdown/markdown-view.component';

const INTRO: Record<string, string> = {
  never_signed: 'Before you start, read the rules of use and agree to them.',
  new_version: 'The rules of use have changed. Read the new version and agree to it to carry on.',
  expired: 'It has been a year since you agreed to the rules of use. Read them again and agree to carry on.',
  signed: 'You have agreed to these rules of use.',
  none_published: 'There are no rules of use to agree to yet.',
};

/**
 * The rules of use (ADR 0014 §3), full page, after sign-in and before anything else.
 * The text is the platform administrator's Markdown, rendered through DOMPurify
 * (shared/markdown). "I agree" is enabled once the text has been scrolled to the end.
 * Until it is signed the API refuses every other call (403 acknowledgement_required).
 */
@Component({
  selector: 'tn-rules-of-use',
  imports: [DatePipe, MatButtonModule, MatCardModule, MatIconModule, MatProgressBarModule, MarkdownViewComponent],
  template: `
    <main class="shell">
      <mat-card class="card">
        <header class="head">
          <mat-icon class="glyph" aria-hidden="true">gavel</mat-icon>
          <div>
            <h1>Rules of use</h1>
            @if (current()?.version; as v) {
              <p class="meta">Version {{ v.version }}, published {{ v.published_at | date: 'longDate' }}</p>
            }
          </div>
        </header>

        @if (loading()) {
          <mat-progress-bar mode="indeterminate" />
        } @else if (error()) {
          <p class="error" role="alert">{{ error() }}</p>
          <div class="actions">
            <button mat-flat-button (click)="load()">Try again</button>
            <button mat-button (click)="signOut()">Sign out</button>
          </div>
        } @else if (current(); as c) {
          <p class="intro">{{ intro(c) }}</p>
          @if (c.signed_at && !c.required) {
            <p class="meta">Agreed on {{ c.signed_at | date: 'medium' }}. You will be asked again by
              {{ c.expires_at | date: 'longDate' }}, or sooner if they change.</p>
          }
          @if (c.version) {
            <div #scroller class="text" tabindex="0" role="document" aria-label="Rules of use text"
                 (scroll)="checkScrolled()">
              <tn-markdown-view [source]="c.version.text" />
            </div>
          }
          <div class="actions">
            @if (c.required && c.version) {
              <button mat-flat-button color="primary" [disabled]="!scrolledToEnd() || signing()" (click)="agree(c)">
                I agree
              </button>
              <button mat-button (click)="signOut()">Sign out</button>
              @if (!scrolledToEnd()) {
                <span class="hint">Scroll to the end of the rules to continue.</span>
              }
            } @else {
              <button mat-flat-button color="primary" (click)="carryOn()">Continue</button>
            }
          </div>
        }
      </mat-card>
    </main>
  `,
  styles: [
    `
      .shell {
        display: flex;
        justify-content: center;
        padding: 2rem 1rem;
        min-height: 100vh;
        box-sizing: border-box;
      }
      .card {
        width: min(820px, 100%);
        padding: 1.5rem;
        display: flex;
        flex-direction: column;
        gap: 0.75rem;
      }
      .head {
        display: flex;
        gap: 0.75rem;
        align-items: center;
      }
      .glyph {
        font-size: 2.25rem;
        width: 2.25rem;
        height: 2.25rem;
        opacity: 0.75;
      }
      h1 {
        margin: 0;
        font-size: 1.5rem;
      }
      .meta {
        margin: 0;
        opacity: 0.7;
        font-size: 0.875rem;
      }
      .intro {
        margin: 0;
      }
      .text {
        max-height: 60vh;
        overflow: auto;
        padding: 0.5rem 1rem;
        border: 1px solid var(--mat-sys-outline-variant, rgba(127, 127, 127, 0.35));
        border-radius: 8px;
      }
      .actions {
        display: flex;
        gap: 0.5rem;
        align-items: center;
        flex-wrap: wrap;
      }
      .hint {
        opacity: 0.7;
        font-size: 0.875rem;
      }
      .error {
        color: var(--mat-sys-error, #b3261e);
      }
    `,
  ],
})
export class RulesOfUseComponent implements OnInit {
  private readonly api = inject(AcknowledgementApiService);
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  readonly current = signal<AcknowledgementCurrent | null>(null);
  readonly loading = signal(true);
  readonly signing = signal(false);
  readonly error = signal<string | null>(null);
  readonly scrolledToEnd = signal(false);

  @ViewChild('scroller') scroller?: ElementRef<HTMLElement>;

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.error.set(null);
    this.scrolledToEnd.set(false);
    this.api.current().subscribe({
      next: c => {
        this.current.set(c);
        this.loading.set(false);
        this.auth.setAcknowledgementRequired(c.required);
        // Text short enough to need no scrolling counts as read once it is on screen.
        setTimeout(() => this.checkScrolled());
      },
      error: () => {
        this.loading.set(false);
        this.error.set('The rules of use could not be loaded.');
      },
    });
  }

  intro(c: AcknowledgementCurrent): string {
    return INTRO[c.reason] ?? INTRO['never_signed'];
  }

  checkScrolled(): void {
    const el = this.scroller?.nativeElement;
    if (!el) return;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 8) {
      this.scrolledToEnd.set(true);
    }
  }

  agree(c: AcknowledgementCurrent): void {
    if (!c.version || this.signing()) return;
    this.signing.set(true);
    this.api.sign(c.version.id).subscribe({
      next: after => {
        this.signing.set(false);
        this.current.set(after);
        this.auth.setAcknowledgementRequired(after.required);
        if (!after.required) this.carryOn();
      },
      error: (err: unknown) => {
        this.signing.set(false);
        if (err instanceof HttpErrorResponse && err.status === 409) {
          // A newer version was published while this one was open: show that one.
          this.load();
          return;
        }
        this.error.set('Your agreement could not be recorded. Try again.');
      },
    });
  }

  carryOn(): void {
    const target = safeReturnUrl(this.route.snapshot.queryParamMap.get('returnUrl'));
    void this.router.navigateByUrl(target && !target.startsWith('/rules-of-use') ? target : '/dashboard');
  }

  signOut(): void {
    this.auth.logout();
  }
}
