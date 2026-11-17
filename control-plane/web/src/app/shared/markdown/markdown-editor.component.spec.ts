import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { ImageUploader, MarkdownEditorComponent } from './markdown-editor.component';

const png = () => new File([new Uint8Array([1, 2, 3])], 'My shot (1).png', { type: 'image/png' });

describe('MarkdownEditorComponent images (#379)', () => {
  function make(upload: ImageUploader | null, hint = '') {
    TestBed.configureTestingModule({ imports: [MarkdownEditorComponent] });
    const f = TestBed.createComponent(MarkdownEditorComponent);
    f.componentInstance.imageUpload = upload;
    f.componentInstance.imageHint = hint;
    f.componentInstance.value = 'Before\n';
    f.detectChanges();
    return f;
  }
  const text = (f: ReturnType<typeof make>) => (f.nativeElement as HTMLElement).textContent ?? '';

  it('has no Image button without an uploader, and shows the hint instead', () => {
    const f = make(null, 'Save the page once to add images.');
    expect(text(f)).not.toContain('Image');
    expect(text(f)).toContain('Save the page once');
  });

  it('inserts the markdown for an uploaded image at the cursor', () => {
    const upload = jasmine.createSpy('upload').and.returnValue(of({ url: '/api/wiki/images/abc', filename: 'My shot (1).png' }));
    const f = make(upload);
    const values: string[] = [];
    f.componentInstance.valueChange.subscribe(v => values.push(v));
    f.componentInstance.area.nativeElement.setSelectionRange(7, 7);
    const input = document.createElement('input');
    Object.defineProperty(input, 'files', { value: [png()] });
    f.componentInstance.pick(input);
    expect(upload).toHaveBeenCalledTimes(1);
    expect(values.at(-1)).toBe('Before\n![My shot  1](/api/wiki/images/abc)\n');
    expect(f.componentInstance.uploading()).toBeFalse();
  });

  it('refuses a non-image before sending anything', () => {
    const upload = jasmine.createSpy('upload');
    const f = make(upload);
    const input = document.createElement('input');
    Object.defineProperty(input, 'files', { value: [new File(['<svg/>'], 'x.svg', { type: 'image/svg+xml' })] });
    f.componentInstance.pick(input);
    expect(upload).not.toHaveBeenCalled();
    expect(f.componentInstance.uploadError()).toContain('PNG, JPEG, GIF or WebP');
  });

  it('shows the server\'s reason when the upload is refused, and leaves the text alone', () => {
    const f = make(() => throwError(() => ({ error: { detail: 'SVG and HTML are not accepted.' } })));
    const input = document.createElement('input');
    Object.defineProperty(input, 'files', { value: [png()] });
    f.componentInstance.pick(input);
    expect(f.componentInstance.uploadError()).toBe('SVG and HTML are not accepted.');
    expect(f.componentInstance.value).toBe('Before\n');
  });

  it('takes a pasted image but leaves pasted text to the browser', () => {
    const upload = jasmine.createSpy('upload').and.returnValue(of({ url: '/api/wiki/images/abc' }));
    const f = make(upload);
    const withImage = { clipboardData: { files: [png()] }, preventDefault: jasmine.createSpy('pd') } as unknown as ClipboardEvent;
    f.componentInstance.onPaste(withImage);
    expect(withImage.preventDefault).toHaveBeenCalled();
    expect(upload).toHaveBeenCalledTimes(1);
    const textOnly = { clipboardData: { files: [] }, preventDefault: jasmine.createSpy('pd') } as unknown as ClipboardEvent;
    f.componentInstance.onPaste(textOnly);
    expect(textOnly.preventDefault).not.toHaveBeenCalled();
  });
});
