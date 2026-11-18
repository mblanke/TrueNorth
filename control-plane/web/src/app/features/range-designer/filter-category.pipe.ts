// Author: Marc-Andre Blanke
// Created: 2026-03-04T22:24:49Z
import { Pipe, PipeTransform } from '@angular/core';

@Pipe({
  name: 'filterCategory',
  pure: true,
})
export class FilterCategoryPipe implements PipeTransform {
  transform(items: any[], category: string): any[] {
    if (!items || !category) return items;
    return items.filter(i => i.category === category);
  }
}