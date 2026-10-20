import { layoutTopology2d } from './topology-2d';

const ZONE = (id: string, label: string, x: number, y: number) => ({
  type: 'standard.Rectangle', id, nodeType: 'subnet', position: { x, y }, size: { width: 400, height: 200 },
  nodeData: { label, cidr: '10.0.0.0/24' },
});

describe('layoutTopology2d', () => {
  it('reads zones, hosts with OS / address / status, and NIC links that end on a zone edge', () => {
    const t = layoutTopology2d({ cells: [
      ZONE('za', 'servers', 0, 300),
      { type: 'standard.Rectangle', id: 'fw', nodeType: 'firewall', position: { x: 100, y: 100 },
        size: { width: 170, height: 64 }, nodeData: { label: 'Core Firewall', os_template: 'pfsense', ip: '10.0.0.1' } },
      { type: 'standard.Rectangle', id: 'dc', nodeType: 'dc', position: { x: 20, y: 352 }, size: { width: 170, height: 64 },
        nodeData: { label: 'Domain Controller', os_template: 'windows-server-2022', ip: '10.0.0.10',
          runtime: { status: 'running', built: true, ip: '10.0.0.77' } } },
      { type: 'standard.Link', id: 'l1', source: { id: 'fw' }, target: { id: 'za' },
        linkData: { network: 'servers', ip: '10.0.0.1', monitor: false } },
    ] });
    expect(t.zones.map(z => z.label)).toEqual(['servers']);
    const dc = t.nodes.find(n => n.id === 'dc')!;
    expect(dc.ip).toBe('10.0.0.77'); // what the build recorded wins over the plan
    expect(dc.status).toBe('running');
    expect(dc.title).toContain('Domain controller');
    expect(dc.title).toContain('windows-server-2022');
    expect(t.links.length).toBe(1);
    const l = t.links[0];
    expect(l.ip).toBe('10.0.0.1');
    expect(l.y2).toBe(300); // the zone's top edge, below the firewall
    expect(l.y1).toBe(164); // the firewall's bottom edge
  });

  it('gives designer cells their default size and frames everything in the viewBox', () => {
    const t = layoutTopology2d({ cells: [
      { id: 'a', nodeType: 'server', position: { x: 10, y: 20 }, nodeData: { label: 'Web' } },
    ] });
    expect(t.nodes[0].w).toBe(120);
    expect(t.nodes[0].h).toBe(80);
    expect(t.nodes[0].status).toBe('');
    expect(t.box).toEqual({ x: -14, y: -4, w: 168, h: 128 });
  });

  it('cuts a long name to the box and keeps the full one in the title', () => {
    const t = layoutTopology2d({ cells: [
      { id: 'a', nodeType: 'server', position: { x: 0, y: 0 }, size: { width: 170, height: 64 },
        nodeData: { label: 'SIEM (OpenSearch + Dashboards) primary node' } },
    ] });
    expect(t.nodes[0].shortLabel.endsWith('…')).toBeTrue();
    expect(t.nodes[0].shortLabel.length).toBeLessThan(25);
    expect(t.nodes[0].title).toContain('SIEM (OpenSearch + Dashboards) primary node');
  });

  it('draws nothing from nothing and skips links to missing cells', () => {
    expect(layoutTopology2d(null).nodes).toEqual([]);
    expect(layoutTopology2d({ cells: 'nope' }).zones).toEqual([]);
    const t = layoutTopology2d({ cells: [{ type: 'standard.Link', id: 'l', source: { id: 'x' }, target: { id: 'y' } }] });
    expect(t.links).toEqual([]);
  });
});
