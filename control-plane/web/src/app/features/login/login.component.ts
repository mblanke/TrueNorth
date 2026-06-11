import { AfterViewInit, Component, ElementRef, NgZone, OnDestroy, ViewChild } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import gsap from 'gsap';

import { AuthService } from '@core/services/auth.service';
import { MotionService } from '../../shared/motion';
import { AuroraScene } from './aurora-scene';

@Component({
  selector: 'tn-login',
  standalone: true,
  imports: [CommonModule, MatButtonModule, MatIconModule],
  template: `
    <div class="hero" #hero>
      <canvas class="hero-canvas" #bg></canvas>

      <div class="hero-card" #card>
        <div class="wordmark" #wordmark aria-label="TrueNorth">
          @for (ch of wordmarkChars; track $index) {
            <span class="wm-char">{{ ch }}</span>
          }
        </div>
        <div class="kicker">TRUENORTH <span class="kicker-sep">//</span> CYBER RANGE</div>

        <p class="tagline">
          Build, run, and score realistic incident-response exercises —
          at enterprise scale.
        </p>

        <button mat-raised-button color="primary" class="sso-btn" #ssoBtn (click)="login()">
          <mat-icon>login</mat-icon>
          Sign in with SSO
        </button>

        <div class="footnote">Authorized personnel only · All activity is monitored</div>
      </div>
    </div>
  `,
  styles: [`
    .hero {
      position: relative;
      width: 100vw;
      height: 100vh;
      overflow: hidden;
      background: var(--bg-primary);
    }
    .hero-canvas {
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      display: block;
    }

    .hero-card {
      position: relative;
      z-index: 1;
      max-width: 460px;
      margin: 0 auto;
      top: 50%;
      transform: translateY(-50%);
      padding: 48px 44px;
      text-align: center;
      border-radius: var(--radius-lg);
      border: 1px solid var(--glass-border);
      background: var(--glass-bg);
      box-shadow: var(--shadow-2);
    }
    @supports (backdrop-filter: blur(1px)) {
      .hero-card { backdrop-filter: blur(18px) saturate(150%); }
    }

    .wordmark {
      font-family: var(--font-display);
      font-size: 44px;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: var(--text-primary);
      line-height: 1.1;
      user-select: none;
    }
    .wm-char { display: inline-block; }
    .wordmark .wm-char:nth-child(-n+4) {
      background: var(--gradient-accent);
      -webkit-background-clip: text;
      background-clip: text;
      -webkit-text-fill-color: transparent;
    }

    .kicker {
      margin-top: 6px;
      font-family: var(--font-display);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 4px;
      color: var(--text-muted);
    }
    .kicker-sep { color: var(--accent); }

    .tagline {
      margin: 22px 0 28px;
      color: var(--text-secondary);
      font-size: 14px;
      line-height: 1.6;
    }

    .sso-btn {
      width: 100%;
      height: 46px;
      font-size: 15px;
      font-weight: 600;
      border-radius: 10px !important;
      box-shadow: var(--glow-accent) !important;
    }
    .sso-btn .mat-icon { margin-right: 8px; }

    .footnote {
      margin-top: 22px;
      font-size: 11px;
      letter-spacing: 0.4px;
      color: var(--text-muted);
    }
  `],
})
export class LoginComponent implements AfterViewInit, OnDestroy {
  @ViewChild('bg') canvasRef!: ElementRef<HTMLCanvasElement>;
  @ViewChild('card') cardRef!: ElementRef<HTMLElement>;
  @ViewChild('wordmark') wordmarkRef!: ElementRef<HTMLElement>;
  @ViewChild('ssoBtn', { read: ElementRef }) ssoBtnRef!: ElementRef<HTMLElement>;

  readonly wordmarkChars = 'TrueNorth'.split('');

  private scene?: AuroraScene;
  private timeline?: gsap.core.Timeline;

  constructor(
    private auth: AuthService,
    private router: Router,
    private route: ActivatedRoute,
    private motion: MotionService,
    private zone: NgZone,
  ) {
    // ?preview=1 keeps the page reachable in dev, where auth is mocked.
    const preview = this.route.snapshot.queryParamMap.has('preview');
    if (this.auth.isAuthenticated() && !preview) {
      this.router.navigate(['/dashboard']);
    }
  }

  ngAfterViewInit(): void {
    const reduced = this.motion.reducedMotion();

    this.zone.runOutsideAngular(() => {
      AuroraScene.create(this.canvasRef.nativeElement, reduced)
        .then((scene) => {
          // Component may have been destroyed while three.js loaded.
          if (this.destroyed) {
            scene.destroy();
          } else {
            this.scene = scene;
          }
        })
        .catch(() => {
          // WebGL unavailable — static CSS background still carries the page.
        });
    });

    this.timeline = this.motion.createTimeline();
    const chars = this.wordmarkRef.nativeElement.querySelectorAll('.wm-char');
    if (reduced) {
      return;
    }
    this.timeline
      .fromTo(
        this.cardRef.nativeElement,
        { autoAlpha: 0, y: 26, scale: 0.97 },
        { autoAlpha: 1, y: 0, scale: 1, duration: 0.6, ease: 'power3.out' },
      )
      .fromTo(
        chars,
        { autoAlpha: 0, y: 14 },
        { autoAlpha: 1, y: 0, duration: 0.4, stagger: 0.035, ease: 'power2.out' },
        '-=0.3',
      )
      .fromTo(
        this.ssoBtnRef.nativeElement,
        { autoAlpha: 0, y: 10 },
        { autoAlpha: 1, y: 0, duration: 0.35, ease: 'power2.out', clearProps: 'all' },
        '-=0.1',
      );
  }

  private destroyed = false;

  ngOnDestroy(): void {
    this.destroyed = true;
    this.timeline?.kill();
    this.scene?.destroy();
  }

  login(): void {
    this.auth.login();
  }
}
