import * as joint from '@joint/core';
import { upgradeDiagramJson, withoutRuntime } from './diagram-compat';

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

describe('withoutRuntime', () => {
  /** The shape GET /ranges/{id}/diagram generates (app/range_diagram.py): zone, host, NIC link. */
  const generated = () => ({
    cells: [
      { type: 'standard.Rectangle', id: 'zone-0', position: { x: 40, y: 200 }, size: { width: 400, height: 150 },
        nodeType: 'subnet', nodeData: { label: 'soc_tools', cidr: '10.60.202.0/24', vlan: 202 },
        attrs: { body: { stroke: '#29B6F6' }, label: { text: 'soc_tools  (10.60.202.0/24)' } } },
      { type: 'standard.Rectangle', id: 'fw01', position: { x: 120, y: 40 }, size: { width: 170, height: 64 },
        nodeType: 'firewall',
        nodeData: { label: 'Core Firewall', os_template: 'pfsense', ip: '10.60.204.1', hostname: 'fw01',
          runtime: { status: 'running', built: true, ip: '10.60.204.1', nics: [] } },
        attrs: { body: { stroke: '#FF7043' }, label: { text: 'Core Firewall\npfsense\n10.60.204.1' } },
        ports: { groups: { in: { position: 'left', label: { position: { name: 'outside' } } } },
          items: [{ group: 'in', id: 'in1' }] } },
      { type: 'standard.Link', id: 'nic-fw01-soc_tools', source: { id: 'fw01' }, target: { id: 'zone-0' }, z: 2,
        linkData: { network: 'soc_tools', ip: '10.60.202.1', monitor: false },
        attrs: { line: { stroke: '#8892a6', strokeWidth: 2, targetMarker: { type: 'path', d: 'M 0 0' } } },
        labels: [{ position: 0.5, attrs: { text: { text: '10.60.202.1' } } }] },
    ],
  });

  it('drops what the build recorded and keeps the design, without mutating the input', () => {
    const input = generated();
    const out = withoutRuntime(input)!;
    expect((out.cells[1] as any).nodeData.runtime).toBeUndefined();
    expect((out.cells[1] as any).nodeData.hostname).toBe('fw01');
    expect(out.cells[0]).toBe(input.cells[0]);
    expect((input.cells[1] as any).nodeData.runtime.status).toBe('running');
    expect(withoutRuntime(null)).toBeNull();
  });

  it('loads into the designer graph, NIC link to the zone included', () => {
    const graph = new joint.dia.Graph({}, { cellNamespace: joint.shapes })
      .fromJSON(upgradeDiagramJson(withoutRuntime(generated())) as any);
    expect(graph.getElements().length).toBe(2);
    const [link] = graph.getLinks();
    expect(link.getTargetElement()?.id).toBe('zone-0');
  });
});
