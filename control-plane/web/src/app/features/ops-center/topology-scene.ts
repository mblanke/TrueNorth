import type * as THREE from 'three';
import type { OrbitControls as OrbitControlsType } from 'three/examples/jsm/controls/OrbitControls.js';
import { nodeIp, nodeStatus } from '../../shared/components/range-topology/topology-2d';

export interface TopologyNode {
  id: string;
  label: string;
  type: string;
  x: number;
  y: number;
  data: Record<string, unknown>;
}

export interface TopologyZone {
  id: string;
  label: string;
  type: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface TopologyLink {
  sourceId: string;
  targetId: string;
}

export interface TopologyGraph {
  nodes: TopologyNode[];
  zones: TopologyZone[];
  links: TopologyLink[];
}

export interface HoverInfo {
  node: TopologyNode;
  clientX: number;
  clientY: number;
}

/** Mirrors the 2D designer's palette so 3D matches what users drew. */
const NODE_COLORS: Record<string, string> = {
  workstation: '#42A5F5', server: '#66BB6A', dc: '#AB47BC', kali: '#EF5350',
  switch: '#FFA726', router: '#26C6DA', cloud: '#78909C',
  firewall: '#FF7043', seconion: '#5C6BC0', subnet: '#29B6F6', dmz: '#FFCA28',
};

/** Vertical size per node family — servers read taller than workstations. */
const NODE_HEIGHTS: Record<string, number> = {
  workstation: 1.0, server: 1.8, dc: 2.2, kali: 1.0,
  switch: 0.5, router: 0.7, cloud: 1.2, firewall: 1.4, seconion: 1.8,
};

const WORLD_SCALE = 1 / 28; // JointJS px → world units

/**
 * Parses a serialized joint.dia.Graph (diagram_json: saved, or generated from the
 * template by GET /ranges/{id}/diagram) into a plain graph. Node x/y is the centre of
 * the cell; a link may end on a zone (a NIC on that network).
 */
export function parseDiagram(diagram: { cells?: unknown } | null | undefined): TopologyGraph {
  const cells: any[] = Array.isArray(diagram?.cells) ? diagram!.cells as any[] : [];
  const nodes: TopologyNode[] = [];
  const zones: TopologyZone[] = [];
  const links: TopologyLink[] = [];

  for (const cell of cells) {
    if (typeof cell.type === 'string' && cell.type.toLowerCase().includes('link')) {
      const s = cell.source?.id;
      const t = cell.target?.id;
      if (s && t) links.push({ sourceId: s, targetId: t });
      continue;
    }
    const nodeType = cell.nodeType || 'workstation';
    const label = cell.nodeData?.label || cell.attrs?.label?.text || nodeType;
    const pos = cell.position ?? { x: 0, y: 0 };
    if (nodeType === 'subnet' || nodeType === 'dmz') {
      const size = cell.size ?? { width: 200, height: 160 };
      zones.push({ id: cell.id, label, type: nodeType, x: pos.x, y: pos.y, width: size.width, height: size.height });
    } else {
      const raw: Record<string, unknown> = cell.nodeData ?? {};
      const size = cell.size ?? { width: 120, height: 80 };
      const data: Record<string, unknown> = {
        ...raw,
        ip: nodeIp(raw) || undefined,
        os: raw['os'] || raw['os_template'] || undefined,
        status: nodeStatus(raw) || undefined,
      };
      nodes.push({ id: cell.id, label, type: nodeType, x: pos.x + size.width / 2, y: pos.y + size.height / 2, data });
    }
  }
  return { nodes, zones, links };
}

/**
 * Read-only 3D network topology view, built from a Range Designer diagram.
 * Lazy-loads three.js; caller runs it outside the Angular zone and must call
 * destroy(). With reduced motion the scene renders on demand (controls still
 * work) instead of running a continuous RAF loop.
 */
export class TopologyScene {
  private renderer!: THREE.WebGLRenderer;
  private scene!: THREE.Scene;
  private camera!: THREE.PerspectiveCamera;
  private controls!: OrbitControlsType;
  private raycaster!: THREE.Raycaster;
  private pointer!: THREE.Vector2;

  private nodeMeshes = new Map<string, THREE.Mesh>();
  /** Half-width (x) and half-depth (z) of the content, in world units. */
  private extent = { x: 5, z: 5 };
  private userMoved = false;
  /** Zone footprints in world units, so a NIC link can end on the zone's edge. */
  private zoneRects = new Map<string, { minX: number; maxX: number; minZ: number; maxZ: number }>();
  private nodeById = new Map<string, TopologyNode>();
  private linkMaterials: THREE.LineBasicMaterial[] = [];
  private hovered?: THREE.Mesh;

  private rafId = 0;
  private startTime = 0;
  private disposed = false;
  private resizeObserver?: ResizeObserver;

  onHover?: (info: HoverInfo | null) => void;

  private constructor(
    private three: typeof THREE,
    private OrbitControls: typeof OrbitControlsType,
    private canvas: HTMLCanvasElement,
    private graph: TopologyGraph,
    private reducedMotion: boolean,
  ) {}

  static async create(
    canvas: HTMLCanvasElement,
    graph: TopologyGraph,
    reducedMotion: boolean,
  ): Promise<TopologyScene> {
    const [three, controlsModule] = await Promise.all([
      import('three'),
      import('three/examples/jsm/controls/OrbitControls.js'),
    ]);
    const scene = new TopologyScene(three, controlsModule.OrbitControls, canvas, graph, reducedMotion);
    scene.build();
    return scene;
  }

  private cssVar(name: string, fallback: string): string {
    return getComputedStyle(document.body).getPropertyValue(name).trim() || fallback;
  }

  private build(): void {
    const T = this.three;
    const parent = this.canvas.parentElement ?? document.body;
    const width = parent.clientWidth || 800;
    const height = parent.clientHeight || 520;
    const bg = new T.Color(this.cssVar('--bg-primary', '#071629'));
    const accent = new T.Color(this.cssVar('--accent', '#1FB6A6'));

    this.renderer = new T.WebGLRenderer({ canvas: this.canvas, antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
    this.renderer.setSize(width, height, false);
    this.renderer.setClearColor(bg, 1);

    this.scene = new T.Scene();
    this.scene.fog = new T.Fog(bg.getHex(), 60, 160);

    this.camera = new T.PerspectiveCamera(55, width / height, 0.1, 400);

    this.scene.add(new T.AmbientLight(0xffffff, 0.75));
    const dir = new T.DirectionalLight(0xffffff, 1.1);
    dir.position.set(12, 30, 18);
    this.scene.add(dir);

    const grid = new T.GridHelper(160, 64, accent.getHex(), 0x1b3a5e);
    (grid.material as THREE.Material).transparent = true;
    (grid.material as THREE.Material).opacity = 0.25;
    this.scene.add(grid);

    // Center the diagram around the origin.
    const xs = [...this.graph.nodes.map(n => n.x), ...this.graph.zones.flatMap(z => [z.x, z.x + z.width])];
    const ys = [...this.graph.nodes.map(n => n.y), ...this.graph.zones.flatMap(z => [z.y, z.y + z.height])];
    const cx = xs.length ? (Math.min(...xs) + Math.max(...xs)) / 2 : 0;
    const cz = ys.length ? (Math.min(...ys) + Math.max(...ys)) / 2 : 0;
    const toWorld = (x: number, y: number): [number, number] =>
      [(x - cx) * WORLD_SCALE, (y - cz) * WORLD_SCALE];

    for (const zone of this.graph.zones) {
      this.addZone(zone, toWorld);
    }
    for (const node of this.graph.nodes) {
      this.addNode(node, toWorld);
      this.nodeById.set(node.id, node);
    }
    for (const link of this.graph.links) {
      this.addLink(link);
    }

    // Content extent in world units (the content is centred on the origin).
    for (const n of this.graph.nodes) {
      const [wx, wz] = toWorld(n.x, n.y);
      this.extent.x = Math.max(this.extent.x, Math.abs(wx) + 1);
      this.extent.z = Math.max(this.extent.z, Math.abs(wz) + 1);
    }
    for (const r of this.zoneRects.values()) {
      this.extent.x = Math.max(this.extent.x, -r.minX, r.maxX);
      this.extent.z = Math.max(this.extent.z, -r.minZ, r.maxZ);
    }
    const span = Math.max(this.extent.x, this.extent.z);
    // Fade only what is well beyond the content, however large the range is.
    this.scene.fog = new T.Fog(bg.getHex(), Math.max(60, span * 3), Math.max(160, span * 6));

    this.controls = new this.OrbitControls(this.camera, this.canvas);
    this.controls.enableDamping = !this.reducedMotion;
    this.controls.dampingFactor = 0.08;
    this.controls.maxPolarAngle = Math.PI / 2.05;
    this.controls.target.set(0, 0, 0);
    this.frame();
    // Once someone orbits or zooms, a resize keeps their view instead of re-framing.
    this.controls.addEventListener('start', () => (this.userMoved = true));

    this.raycaster = new this.three.Raycaster();
    this.pointer = new this.three.Vector2(-10, -10);
    this.canvas.addEventListener('pointermove', this.onPointerMove, { passive: true });

    this.resizeObserver = new ResizeObserver(() => this.onResize());
    this.resizeObserver.observe(parent);

    this.startTime = performance.now();
    if (this.reducedMotion) {
      this.controls.addEventListener('change', this.renderOnce);
      this.renderOnce();
    } else {
      this.loop();
    }
  }

  private addZone(zone: TopologyZone, toWorld: (x: number, y: number) => [number, number]): void {
    const T = this.three;
    const color = new T.Color(NODE_COLORS[zone.type] ?? '#29B6F6');
    const w = zone.width * WORLD_SCALE;
    const d = zone.height * WORLD_SCALE;
    const [wx, wz] = toWorld(zone.x + zone.width / 2, zone.y + zone.height / 2);

    const slab = new T.Mesh(
      new T.BoxGeometry(w, 0.12, d),
      new T.MeshLambertMaterial({ color, transparent: true, opacity: 0.14 }),
    );
    slab.position.set(wx, 0.06, wz);
    this.scene.add(slab);

    const edges = new T.LineSegments(
      new T.EdgesGeometry(new T.BoxGeometry(w, 0.12, d)),
      new T.LineBasicMaterial({ color, transparent: true, opacity: 0.6 }),
    );
    edges.position.copy(slab.position);
    this.scene.add(edges);
    this.zoneRects.set(zone.id, { minX: wx - w / 2, maxX: wx + w / 2, minZ: wz - d / 2, maxZ: wz + d / 2 });

    // At the zone's back-left corner, left-aligned, so it never sits on a host's label.
    const name = this.addLabel(zone.label, wx - w / 2 + 0.2, 0.3, wz - d / 2 - 0.7, 1.4, 0.85, '', true);
    name.center.set(0, 0.5);
  }

  private addNode(node: TopologyNode, toWorld: (x: number, y: number) => [number, number]): void {
    const T = this.three;
    const color = new T.Color(NODE_COLORS[node.type] ?? '#42A5F5');
    const h = NODE_HEIGHTS[node.type] ?? 1.2;
    const [wx, wz] = toWorld(node.x, node.y);

    const mesh = new T.Mesh(
      new T.BoxGeometry(1.6, h, 1.6),
      new T.MeshLambertMaterial({ color, emissive: color, emissiveIntensity: 0.18 }),
    );
    mesh.position.set(wx, h / 2 + 0.12, wz);
    mesh.userData['nodeId'] = node.id;
    this.scene.add(mesh);
    this.nodeMeshes.set(node.id, mesh);

    const edges = new T.LineSegments(
      new T.EdgesGeometry(mesh.geometry),
      new T.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.25 }),
    );
    edges.position.copy(mesh.position);
    this.scene.add(edges);

    const ip = typeof node.data['ip'] === 'string' ? node.data['ip'] : '';
    this.addLabel(node.label, wx, h + 1.0, wz, 1.0, 0.95, ip);
  }

  /** A billboard label; `sub` (an address) goes on a second, monospaced line. */
  private addLabel(
    text: string, x: number, y: number, z: number, scale: number, opacity: number, sub = '', onTop = false,
  ): THREE.Sprite {
    const T = this.three;
    const cnv = document.createElement('canvas');
    const ctx = cnv.getContext('2d')!;
    const font = '600 28px "Inter", sans-serif';
    const subFont = '500 22px "JetBrains Mono", monospace';
    ctx.font = font;
    let w = ctx.measureText(text).width;
    if (sub) {
      ctx.font = subFont;
      w = Math.max(w, ctx.measureText(sub).width);
    }
    w = Math.ceil(w) + 28;
    const lineH = 44;
    const height = sub ? lineH + 30 : lineH;
    cnv.width = w;
    cnv.height = height;
    ctx.fillStyle = 'rgba(5, 12, 24, 0.65)';
    ctx.fillRect(0, 0, w, height);
    // The plate is always dark, so the text is always light (whatever the page theme).
    ctx.fillStyle = '#F0F4F8';
    ctx.textBaseline = 'middle';
    ctx.font = font;
    ctx.fillText(text, 14, 23);
    if (sub) {
      ctx.font = subFont;
      ctx.fillStyle = '#B8C4D4';
      ctx.fillText(sub, 14, lineH + 10);
    }

    const texture = new T.CanvasTexture(cnv);
    const sprite = new T.Sprite(new T.SpriteMaterial({
      map: texture, transparent: true, opacity, depthWrite: false, depthTest: !onTop,
    }));
    if (onTop) sprite.renderOrder = 10;
    sprite.scale.set((w / lineH) * scale, (height / lineH) * scale, 1);
    sprite.position.set(x, y, z);
    this.scene.add(sprite);
    return sprite;
  }

  private addLink(link: TopologyLink): void {
    const T = this.three;
    const a = this.endpoint(link.sourceId, link.targetId);
    const b = this.endpoint(link.targetId, link.sourceId);
    if (!a || !b) return;
    const material = new T.LineBasicMaterial({
      color: new T.Color(this.cssVar('--accent', '#1FB6A6')),
      transparent: true,
      opacity: 0.55,
    });
    const geometry = new T.BufferGeometry().setFromPoints([a.setY(0.25), b.setY(0.25)]);
    this.scene.add(new T.Line(geometry, material));
    this.linkMaterials.push(material);
  }

  /** Where a link ends: a node's centre, or the point of a zone's edge nearest the other end. */
  private endpoint(id: string, otherId: string): THREE.Vector3 | null {
    const mesh = this.nodeMeshes.get(id);
    if (mesh) return mesh.position.clone();
    const rect = this.zoneRects.get(id);
    const other = this.nodeMeshes.get(otherId);
    if (!rect || !other) return null;
    const { x, z } = other.position;
    return new this.three.Vector3(
      Math.min(Math.max(x, rect.minX), rect.maxX), 0, Math.min(Math.max(z, rect.minZ), rect.maxZ),
    );
  }

  private onPointerMove = (e: PointerEvent): void => {
    const rect = this.canvas.getBoundingClientRect();
    this.pointer.set(
      ((e.clientX - rect.left) / rect.width) * 2 - 1,
      -((e.clientY - rect.top) / rect.height) * 2 + 1,
    );
    this.pickHover(e.clientX, e.clientY);
    if (this.reducedMotion) this.renderOnce();
  };

  private pickHover(clientX: number, clientY: number): void {
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const hits = this.raycaster.intersectObjects([...this.nodeMeshes.values()]);
    const mesh = hits[0]?.object as THREE.Mesh | undefined;

    if (mesh !== this.hovered) {
      if (this.hovered) {
        this.hovered.scale.setScalar(1);
        (this.hovered.material as THREE.MeshLambertMaterial).emissiveIntensity = 0.18;
      }
      this.hovered = mesh;
      if (mesh) {
        mesh.scale.setScalar(1.15);
        (mesh.material as THREE.MeshLambertMaterial).emissiveIntensity = 0.5;
        const node = this.nodeById.get(mesh.userData['nodeId']);
        if (node) this.onHover?.({ node, clientX, clientY });
      } else {
        this.onHover?.(null);
      }
    } else if (mesh) {
      const node = this.nodeById.get(mesh.userData['nodeId']);
      if (node) this.onHover?.({ node, clientX, clientY });
    }
  }

  /**
   * Put the whole topology in view for the canvas's current shape: seen from the front,
   * about 50 degrees up, slightly to the side, close enough that labels can be read.
   */
  private frame(): void {
    const T = this.three;
    const vHalf = T.MathUtils.degToRad(this.camera.fov / 2);
    const hHalf = Math.atan(Math.tan(vHalf) * this.camera.aspect);
    const elevation = T.MathUtils.degToRad(50);
    // Width must fit across; depth, foreshortened by the elevation, must fit up and down.
    // The near edge is closer to the camera than the target, so it must fit at its own distance.
    const near = this.extent.z * Math.cos(elevation);
    const forWidth = (this.extent.x * 1.1) / Math.tan(hHalf) + near;
    const forDepth = (this.extent.z * Math.sin(elevation) * 1.15 + 3) / Math.tan(vHalf) + near * 0.5;
    const distance = Math.max(26, forWidth, forDepth);
    const dir = new T.Vector3(0.18, Math.sin(elevation), Math.cos(elevation)).normalize();
    this.camera.position.copy(dir.multiplyScalar(distance));
    this.controls.target.set(0, 0, 0);
    this.controls.update();
  }

  private onResize(): void {
    if (this.disposed) return;
    const parent = this.canvas.parentElement ?? document.body;
    const width = parent.clientWidth || 800;
    const height = parent.clientHeight || 520;
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    if (!this.userMoved) this.frame();
    this.renderer.setSize(width, height, false);
    if (this.reducedMotion) this.renderOnce();
  }

  private renderOnce = (): void => {
    if (this.disposed) return;
    this.renderer.render(this.scene, this.camera);
  };

  private loop = (): void => {
    if (this.disposed) return;
    this.rafId = requestAnimationFrame(this.loop);
    const t = (performance.now() - this.startTime) / 1000;
    const pulse = 0.4 + 0.25 * (0.5 + 0.5 * Math.sin(t * 1.6));
    for (const mat of this.linkMaterials) {
      mat.opacity = pulse;
    }
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  };

  destroy(): void {
    if (this.disposed) return;
    this.disposed = true;
    cancelAnimationFrame(this.rafId);
    this.canvas.removeEventListener('pointermove', this.onPointerMove);
    this.resizeObserver?.disconnect();
    this.controls.dispose();
    this.scene.traverse((obj) => {
      const mesh = obj as THREE.Mesh;
      if (mesh.geometry) mesh.geometry.dispose();
      const material = mesh.material as THREE.Material | THREE.Material[] | undefined;
      if (Array.isArray(material)) {
        material.forEach((m) => m.dispose());
      } else if (material) {
        const map = (material as THREE.SpriteMaterial).map;
        if (map) map.dispose();
        material.dispose();
      }
    });
    this.renderer.dispose();
    this.renderer.forceContextLoss();
  }
}
