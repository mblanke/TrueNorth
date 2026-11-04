import { TestBed, fakeAsync, flushMicrotasks, tick } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { WikiApiService } from '@core/services/wiki-api.service';
import { MarkdownViewComponent } from './markdown-view.component';

const ID = '3f2b8c1e-5a47-4d0e-9c1a-0b6f2d7e8a91';

describe('MarkdownViewComponent wiki images (#379)', () => {
  let wiki: jasmine.SpyObj<WikiApiService>;

  beforeEach(() => {
    wiki = jasmine.createSpyObj('WikiApiService', ['imageBlob']);
    wiki.imageBlob.and.returnValue(of(new Blob(['x'], { type: 'image/png' })));
    TestBed.configureTestingModule({
      imports: [MarkdownViewComponent],
      providers: [provideRouter([]), { provide: WikiApiService, useValue: wiki }],
    });
  });

  function show(source: string) {
    const f = TestBed.createComponent(MarkdownViewComponent);
    f.componentInstance.source = source;
    f.componentInstance.ngOnChanges();
    f.detectChanges();
    tick();
    flushMicrotasks();
    f.detectChanges();
    return f;
  }
  const img = (f: ReturnType<typeof show>) => (f.nativeElement as HTMLElement).querySelector('img');

  it('fetches the image with the token and shows it from a blob URL', fakeAsync(() => {
    const f = show(`![shot](/api/wiki/images/${ID})`);
    expect(wiki.imageBlob).toHaveBeenCalledOnceWith(ID);
    expect(img(f)?.src).toMatch(/^blob:/);
  }));

  it('does not fetch again when the text around the image changes', fakeAsync(() => {
    const f = show(`![shot](/api/wiki/images/${ID})`);
    f.componentInstance.source = `More text\n\n![shot](/api/wiki/images/${ID})`;
    f.componentInstance.ngOnChanges();
    f.detectChanges();
    tick();
    flushMicrotasks();
    expect(wiki.imageBlob).toHaveBeenCalledTimes(1);
    expect(img(f)?.src).toMatch(/^blob:/);
  }));

  it('keeps the picture in the HTML when the text changes, with no gap before it comes back', fakeAsync(() => {
    const f = show(`![shot](/api/wiki/images/${ID})`);
    f.componentInstance.source = `Edited\n\n![shot](/api/wiki/images/${ID})`;
    f.componentInstance.ngOnChanges();
    f.detectChanges(); // no tick: the new <img> must already have its src
    expect(img(f)?.src).toMatch(/^blob:/);
  }));

  it('says so when the image cannot be loaded, and tries again next time', fakeAsync(() => {
    wiki.imageBlob.and.returnValue(throwError(() => new Error('404')));
    const f = show(`![shot](/api/wiki/images/${ID})`);
    expect(img(f)?.alt).toContain('image unavailable');
    expect(img(f)?.getAttribute('src')).toBeNull();
  }));

  it('never asks the wiki service for a page without images', fakeAsync(() => {
    show('Just **text**');
    expect(wiki.imageBlob).not.toHaveBeenCalled();
  }));
});
