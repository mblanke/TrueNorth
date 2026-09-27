import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import { EnterStaggerDirective } from './enter-stagger.directive';

/** A list that is empty on first render and filled later, like every API-backed list. */
@Component({
  standalone: true,
  imports: [EnterStaggerDirective],
  template: `
    <div class="list" tnEnterStagger>
      @for (item of items(); track item) {
        <div class="tn-stagger-item row">{{ item }}</div>
      }
    </div>
  `,
})
class LateListComponent {
  readonly items = signal<string[]>([]);
}

describe('EnterStaggerDirective', () => {
  it('reveals items that arrive after first render', async () => {
    const fixture = TestBed.createComponent(LateListComponent);
    fixture.detectChanges(); // empty at ngAfterViewInit, as with an API-backed list

    fixture.componentInstance.items.set(['a', 'b', 'c']);
    fixture.detectChanges();
    await new Promise(resolve => setTimeout(resolve)); // let the MutationObserver fire

    const rows = Array.from(fixture.nativeElement.querySelectorAll('.row')) as HTMLElement[];
    expect(rows.length).toBe(3);
    // The pre-hide class is what kept late items at opacity 0 forever.
    expect(rows.every(r => !r.classList.contains('tn-stagger-item'))).toBeTrue();
  });

  it('still reveals items present on first render', () => {
    const fixture = TestBed.createComponent(LateListComponent);
    fixture.componentInstance.items.set(['x']);
    fixture.detectChanges();
    const row = fixture.nativeElement.querySelector('.row') as HTMLElement;
    expect(row.classList.contains('tn-stagger-item')).toBeFalse();
  });
});
