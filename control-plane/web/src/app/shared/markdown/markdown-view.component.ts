import {
  ChangeDetectionStrategy, Component, ElementRef, HostListener, Injector, Input, OnChanges, OnDestroy, inject,
} from '@angular/core';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { Router } from '@angular/router';
import { WikiApiService } from '@core/services/wiki-api.service';
import { WIKI_IMAGE_ATTR, isAppPath, renderMarkdown } from './markdown';

/** Renders user-written markdown. Sanitised by DOMPurify in `renderMarkdown`. */
@Component({
  selector: 'tn-markdown-view',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<div class="tn-md" [innerHTML]="html"></div>`,
})
export class MarkdownViewComponent implements OnChanges, OnDestroy {
  @Input() source: string | null | undefined = '';
  html: SafeHtml = '';
  private readonly sanitizer = inject(DomSanitizer);
  private readonly router = inject(Router);
  private readonly injector = inject(Injector);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly blobUrls = new Map<string, Promise<string>>();
  private generation = 0;

  ngOnDestroy(): void {
    this.generation++;
    for (const p of this.blobUrls.values()) void p.then(u => URL.revokeObjectURL(u), () => undefined);
    this.blobUrls.clear();
  }

  ngOnChanges(): void {
    // Angular's own sanitiser would drop target/rel on links; DOMPurify has already
    // removed everything dangerous, so its output is what gets trusted here.
    this.html = this.sanitizer.bypassSecurityTrustHtml(renderMarkdown(this.source));
    const gen = ++this.generation;
    // The new HTML is in the DOM after this change-detection pass.
    setTimeout(() => { if (gen === this.generation) this.fillImages(gen); });
  }

  /** Wiki images need the user's token: fetch each one and point its <img> at the blob. */
  private fillImages(gen: number): void {
    const imgs = Array.from(this.host.nativeElement.querySelectorAll<HTMLImageElement>(`img[${WIKI_IMAGE_ATTR}]`));
    if (!imgs.length) return;
    const wiki = this.injector.get(WikiApiService);  // looked up only when a page has images
    for (const img of imgs) {
      const id = img.getAttribute(WIKI_IMAGE_ATTR) ?? '';
      let url = this.blobUrls.get(id);
      if (!url) {
        url = new Promise<string>((resolve, reject) =>
          wiki.imageBlob(id).subscribe({ next: b => resolve(URL.createObjectURL(b)), error: reject }));
        this.blobUrls.set(id, url);
      }
      url.then(
        u => { if (gen === this.generation) img.src = u; },
        () => { this.blobUrls.delete(id); img.alt = img.alt ? `${img.alt} (image unavailable)` : 'Image unavailable'; },
      );
    }
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
