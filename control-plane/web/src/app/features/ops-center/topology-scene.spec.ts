import { parseDiagram } from './topology-scene';

describe('parseDiagram (3D topology)', () => {
  const generated = {
    cells: [
      { type: 'standard.Rectangle', id: 'zone-0', nodeType: 'subnet', position: { x: 40, y: 200 },
        size: { width: 400, height: 150 }, nodeData: { label: 'soc_tools', cidr: '10.60.202.0/24' } },
      { type: 'standard.Rectangle', id: 'siem01', nodeType: 'server', position: { x: 60, y: 252 },
        size: { width: 170, height: 64 },
        nodeData: { label: 'SIEM', os_template: 'ubuntu-2404', ip: '10.60.202.10',
          runtime: { status: 'running', built: true, ip: '10.60.202.99' } } },
      { type: 'standard.Link', id: 'nic', source: { id: 'fw01' }, target: { id: 'zone-0' } },
    ],
  };

  it('places a host at the centre of its cell, inside its zone', () => {
    const g = parseDiagram(generated);
    const siem = g.nodes[0];
    expect([siem.x, siem.y]).toEqual([145, 284]);
    const z = g.zones[0];
    expect(siem.x > z.x && siem.x < z.x + z.width && siem.y > z.y && siem.y < z.y + z.height).toBeTrue();
  });

  it('gives the tooltip the OS, the recorded address and the status', () => {
    const { data } = parseDiagram(generated).nodes[0];
    expect(data['os']).toBe('ubuntu-2404');
    expect(data['ip']).toBe('10.60.202.99');
    expect(data['status']).toBe('running');
  });

  it('keeps links that end on a zone (a NIC on that network)', () => {
    expect(parseDiagram(generated).links).toEqual([{ sourceId: 'fw01', targetId: 'zone-0' }]);
  });

  it('reads nothing from nothing', () => {
    expect(parseDiagram(null)).toEqual({ nodes: [], zones: [], links: [] });
    expect(parseDiagram({ cells: 'x' as unknown })).toEqual({ nodes: [], zones: [], links: [] });
  });
});
