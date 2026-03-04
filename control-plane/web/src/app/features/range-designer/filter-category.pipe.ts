import { Pipe, PipeTransform } from '@angular/core';

@Pipe({
  name: 'filterCategory',
  standalone: true,
  pure: true,
})
export class FilterCategoryPipe implements PipeTransform {
  transform(items: any[], category: string): any[] {
    if (!items || !category) return items;
    return items.filter(i => i.category === category);
  }
}