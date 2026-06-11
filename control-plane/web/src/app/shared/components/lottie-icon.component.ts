import { Component, Input, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { LottieComponent, AnimationOptions } from 'ngx-lottie';

import { MotionService } from '../motion';

/**
 * Thin wrapper around ng-lottie for local vector animations
 * (src/assets/lottie/*.json). Honors prefers-reduced-motion by rendering
 * the first frame statically instead of looping.
 *
 * Usage: <tn-lottie name="radar-scan" [size]="120" />
 */
@Component({
  selector: 'tn-lottie',
  standalone: true,
  imports: [CommonModule, LottieComponent],
  template: `
    <ng-lottie
      [options]="options"
      [width]="size + 'px'"
      [height]="size + 'px'"
    />
  `,
  styles: [`:host { display: inline-block; line-height: 0; }`],
})
export class LottieIconComponent implements OnInit {
  @Input({ required: true }) name!: string;
  @Input() size = 96;
  @Input() loop = true;

  options!: AnimationOptions;

  constructor(private motion: MotionService) {}

  ngOnInit(): void {
    const reduced = this.motion.reducedMotion();
    this.options = {
      path: `assets/lottie/${this.name}.json`,
      autoplay: !reduced,
      loop: this.loop && !reduced,
    };
  }
}
