import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { NotificationService } from '@core/services/notification.service';
import { WikiApiService, WikiImage, WikiPage } from '@core/services/wiki-api.service';
import { WikiEditComponent } from './wiki-edit.component';

const image = (id: string): WikiImage => ({
  id, page_id: 'p1', filename: `${id}.png`, content_type: 'image/png', size_bytes: 2048, width: 4, height: 3, url: `/api/wiki/images/${id}`,
});

describe('WikiEditComponent images (#379)', () => {
  let wiki: jasmine.SpyObj<WikiApiService>;
  let notify: jasmine.SpyObj<NotificationService>;

  function make(pageId: string | null) {
    wiki = jasmine.createSpyObj('WikiApiService', ['getPage', 'tree', 'imagesOf', 'deleteImage', 'uploadImage']);
    notify = jasmine.createSpyObj('NotificationService', ['error', 'success']);
    wiki.getPage.and.returnValue(of({ id: 'p1', title: 'T', body: 'b', tags: '', is_published: true, revision_number: 1, parent_id: null } as unknown as WikiPage));
    wiki.tree.and.returnValue(of([]));
    wiki.imagesOf.and.returnValue(of([image('a'), image('b')]));
    wiki.deleteImage.and.returnValue(of(undefined));
    wiki.uploadImage.and.returnValue(of(image('c')));
    TestBed.configureTestingModule({
      imports: [WikiEditComponent],
      providers: [
        provideRouter([]),
        { provide: WikiApiService, useValue: wiki },
        { provide: NotificationService, useValue: notify },
        {
          provide: ActivatedRoute,
          useValue: {
            parent: { snapshot: { paramMap: convertToParamMap({ space: 'help' }) } },
            snapshot: { paramMap: convertToParamMap(pageId ? { pageId } : {}), queryParamMap: convertToParamMap({}) },
          },
        },
      ],
    });
    const f = TestBed.createComponent(WikiEditComponent);
    f.detectChanges();
    return f;
  }

  it('offers no uploader on a page that has not been saved, and says why', () => {
    const f = make(null);
    expect(f.componentInstance.uploader).toBeNull();
    expect((f.nativeElement as HTMLElement).textContent).toContain('Save the page once to add images');
    expect(wiki.imagesOf).not.toHaveBeenCalled();
  });

  it('lists the page\'s images and removes one for good', () => {
    const f = make('p1');
    expect(f.componentInstance.images().map(i => i.id)).toEqual(['a', 'b']);
    f.componentInstance.removeImage(image('a'));
    expect(wiki.deleteImage).toHaveBeenCalledWith('a');
    expect(f.componentInstance.images().map(i => i.id)).toEqual(['b']);
  });

  it('keeps the image listed and says so when removal fails', () => {
    const f = make('p1');
    wiki.deleteImage.and.returnValue(throwError(() => new Error('403')));
    f.componentInstance.removeImage(image('a'));
    expect(f.componentInstance.images().length).toBe(2);
    expect(notify.error).toHaveBeenCalled();
  });

  it('adds an uploaded image to the list as well as to the text', () => {
    const f = make('p1');
    f.componentInstance.uploader!(new File(['x'], 'c.png', { type: 'image/png' })).subscribe();
    expect(wiki.uploadImage).toHaveBeenCalledWith('p1', jasmine.any(File));
    expect(f.componentInstance.images().map(i => i.id)).toEqual(['a', 'b', 'c']);
  });
});
