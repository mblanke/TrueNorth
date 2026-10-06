import * as joint from '@joint/core';
import { upgradeDiagramJson } from './diagram-compat';

/** A node as jointjs 3 serialised it, and as range_topology.py generated it. */
function legacyDiagram() {
  return {
    cells: [
      {
        type: 'standard.Rectangle',
        id: 'n1',
        position: { x: 10, y: 10 },
        size: { width: 120, height: 60 },
        ports: {
          groups: {
            in: { position: 'left', label: { position: 'outside' } },
            out: { position: 'right', label: { position: 'outside' } },
          },
          items: [
            { group: 'in', id: 'in1' },
            { group: 'out', id: 'out1', label: { position: 'inside' } },
          ],
        },
        attrs: { label: { text: 'web-01' } },
      },
    ],
  };
}

describe('upgradeDiagramJson', () => {
  const load = (json: any) => new joint.dia.Graph({}, { cellNamespace: joint.shapes }).fromJSON(json);

  it('documents the break: @joint/core refuses a legacy diagram as-is', () => {
    expect(() => load(legacyDiagram())).toThrowError(/label position value has an invalid type/);
  });

  it('makes a legacy diagram loadable without losing its nodes or ports', () => {
    const graph = load(upgradeDiagramJson(legacyDiagram()));

    const [node] = graph.getElements();
    expect(node.id).toBe('n1');
    expect(node.attr('label/text')).toBe('web-01');
    expect(node.getPorts().map(p => p.id)).toEqual(['in1', 'out1']);
  });

  it('rewrites group and item label positions to the object form', () => {
    const ports = upgradeDiagramJson(legacyDiagram()).cells[0].ports as any;

    expect(ports.groups.in.label.position).toEqual({ name: 'outside' });
    expect(ports.items[1].label.position).toEqual({ name: 'inside' });
    expect(ports.groups.in.position).toBe('left');
  });

  it('does not mutate its input and passes through anything else', () => {
    const legacy = legacyDiagram();
    upgradeDiagramJson(legacy);
    expect(legacy.cells[0].ports.groups.in.label.position).toBe('outside');

    expect(upgradeDiagramJson({ cells: [{ type: 'standard.Link', id: 'l1' }] })).toEqual({
      cells: [{ type: 'standard.Link', id: 'l1' }],
    });
    expect(upgradeDiagramJson(null)).toBeNull();
  });
});
