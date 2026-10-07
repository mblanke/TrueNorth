import { ChangeDetectionStrategy, Component, HostListener, Input, OnChanges, inject } from '@angular/core';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { Router } from '@angular/router';
import { isAppPath, renderMarkdown } from './markdown';

/** Renders user-written markdown. Sanitised by DOMPurify in `renderMarkdown`. */
@Component({
  selector: 'tn-markdown-view',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<div class="tn-md" [innerHTML]="html"></div>`,
})
export class MarkdownViewComponent implements OnChanges {
  @Input() source: string | null | undefined = '';
  html: SafeHtml = '';
  private readonly sanitizer = inject(DomSanitizer);
  private readonly router = inject(Router);

  ngOnChanges(): void {
    // Angular's own sanitiser would drop target/rel on links; DOMPurify has already
    // removed everything dangerous, so its output is what gets trusted here.
    this.html = this.sanitizer.bypassSecurityTrustHtml(renderMarkdown(this.source));
  }

  /**
   * In-app links (`/wiki/...`, `/support`) route inside the SPA instead of reloading it.
   * Links are real anchors, so keyboard Enter arrives here as a click too.
   */
  @HostListener('click', ['$event'])
  follow(ev: MouseEvent): void {
    const a = (ev.target as HTMLElement | null)?.closest('a');
    const href = a?.getAttribute('href');
    if (!href || !isAppPath(href)) return;
    if (ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    ev.preventDefault();
    this.router.navigateByUrl(href);
  }
}
