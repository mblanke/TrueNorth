import type * as THREE from 'three';

/**
 * "True North Aurora Field" — procedural polar night sky for the login hero.
 *
 * Starfield + three additive aurora ribbons (vertex-displaced planes) + a
 * rotating wireframe north-star polyhedron with a canvas-gradient glow.
 * Zero external assets. three.js is loaded with a runtime dynamic import so
 * it ships as its own lazy chunk, fetched only when this scene is built.
 *
 * Framework-free on purpose: the caller starts it inside
 * NgZone.runOutsideAngular() and must call destroy() on teardown.
 */
export class AuroraScene {
  private renderer!: THREE.WebGLRenderer;
  private scene!: THREE.Scene;
  private camera!: THREE.PerspectiveCamera;
  private stars!: THREE.Points;
  private star!: THREE.LineSegments;
  private glow!: THREE.Sprite;
  private ribbons: THREE.Mesh[] = [];
  private ribbonMaterials: THREE.ShaderMaterial[] = [];

  private rafId = 0;
  private startTime = 0;
  private pointerX = 0;
  private pointerY = 0;
  private resizeObserver?: ResizeObserver;
  private themeObserver?: MutationObserver;
  private disposed = false;

  private constructor(
    private three: typeof THREE,
    private canvas: HTMLCanvasElement,
    private reducedMotion: boolean,
  ) {}

  static async create(canvas: HTMLCanvasElement, reducedMotion: boolean): Promise<AuroraScene> {
    const three = await import('three');
    const scene = new AuroraScene(three, canvas, reducedMotion);
    scene.build();
    return scene;
  }

  private cssColor(name: string, fallback: string): THREE.Color {
    const raw = getComputedStyle(document.body).getPropertyValue(name).trim();
    try {
      return new this.three.Color(raw || fallback);
    } catch {
      return new this.three.Color(fallback);
    }
  }

  private build(): void {
    const T = this.three;
    const parent = this.canvas.parentElement ?? document.body;
    const width = parent.clientWidth || window.innerWidth;
    const height = parent.clientHeight || window.innerHeight;

    this.renderer = new T.WebGLRenderer({ canvas: this.canvas, antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
    this.renderer.setSize(width, height, false);

    this.scene = new T.Scene();
    this.camera = new T.PerspectiveCamera(60, width / height, 0.1, 200);
    this.camera.position.set(0, 2, 28);

    this.buildStars();
    this.buildRibbons();
    this.buildNorthStar();
    this.applyThemeColors();

    this.resizeObserver = new ResizeObserver(() => this.onResize());
    this.resizeObserver.observe(parent);

    this.themeObserver = new MutationObserver(() => this.applyThemeColors());
    this.themeObserver.observe(document.body, { attributes: true, attributeFilter: ['class'] });

    if (!this.reducedMotion) {
      window.addEventListener('pointermove', this.onPointerMove, { passive: true });
      this.startTime = performance.now();
      this.loop();
    } else {
      // Static painted sky: exactly one frame.
      this.renderer.render(this.scene, this.camera);
    }
  }

  private buildStars(): void {
    const T = this.three;
    const COUNT = 1200;
    const positions = new Float32Array(COUNT * 3);
    for (let i = 0; i < COUNT; i++) {
      // Shell distribution between r=60 and r=90, upper hemisphere biased.
      const r = 60 + Math.random() * 30;
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      positions[i * 3] = r * Math.sin(phi) * Math.cos(theta);
      positions[i * 3 + 1] = Math.abs(r * Math.cos(phi)) - 20;
      positions[i * 3 + 2] = r * Math.sin(phi) * Math.sin(theta) - 30;
    }
    const geo = new T.BufferGeometry();
    geo.setAttribute('position', new T.BufferAttribute(positions, 3));

    // Circular sprite so stars render as soft dots instead of squares.
    const starCanvas = document.createElement('canvas');
    starCanvas.width = starCanvas.height = 32;
    const sctx = starCanvas.getContext('2d')!;
    const sgrad = sctx.createRadialGradient(16, 16, 0, 16, 16, 16);
    sgrad.addColorStop(0, 'rgba(255,255,255,1)');
    sgrad.addColorStop(0.4, 'rgba(255,255,255,0.6)');
    sgrad.addColorStop(1, 'rgba(255,255,255,0)');
    sctx.fillStyle = sgrad;
    sctx.fillRect(0, 0, 32, 32);

    const mat = new T.PointsMaterial({
      size: 1.6,
      sizeAttenuation: true,
      map: new T.CanvasTexture(starCanvas),
      transparent: true,
      opacity: 0.8,
      blending: T.AdditiveBlending,
      depthWrite: false,
    });
    this.stars = new T.Points(geo, mat);
    this.scene.add(this.stars);
  }

  private buildRibbons(): void {
    const T = this.three;
    for (let i = 0; i < 3; i++) {
      const material = new T.ShaderMaterial({
        transparent: true,
        depthWrite: false,
        blending: T.AdditiveBlending,
        side: T.DoubleSide,
        uniforms: {
          uTime: { value: 0 },
          uOffset: { value: i * 2.4 },
          uColorA: { value: new T.Color('#1FB6A6') },
          uColorB: { value: new T.Color('#3BD6C4') },
        },
        vertexShader: `
          uniform float uTime;
          uniform float uOffset;
          varying vec2 vUv;
          void main() {
            vUv = uv;
            vec3 p = position;
            float t = uTime * 0.35 + uOffset;
            p.y += sin(p.x * 0.10 + t) * 2.6
                 + sin(p.x * 0.23 + t * 1.7) * 1.2
                 + sin(p.x * 0.05 - t * 0.6) * 3.0;
            gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
          }
        `,
        fragmentShader: `
          uniform vec3 uColorA;
          uniform vec3 uColorB;
          varying vec2 vUv;
          void main() {
            float band = smoothstep(0.0, 0.45, vUv.y) * (1.0 - smoothstep(0.55, 1.0, vUv.y));
            vec3 col = mix(uColorA, uColorB, vUv.y);
            gl_FragColor = vec4(col, band * 0.16);
          }
        `,
      });
      const mesh = new T.Mesh(new T.PlaneGeometry(140, 30, 96, 1), material);
      mesh.position.set(0, 8 + i * 5, -40 - i * 8);
      mesh.rotation.x = -0.6;
      this.ribbons.push(mesh);
      this.ribbonMaterials.push(material);
      this.scene.add(mesh);
    }
  }

  private buildNorthStar(): void {
    const T = this.three;
    const edges = new T.EdgesGeometry(new T.IcosahedronGeometry(2.2, 1));
    this.star = new T.LineSegments(
      edges,
      new T.LineBasicMaterial({ transparent: true, opacity: 0.9 }),
    );
    this.star.position.set(0, 10, -12);
    this.scene.add(this.star);

    // Radial-gradient glow sprite drawn to an offscreen canvas (no assets).
    const size = 128;
    const cnv = document.createElement('canvas');
    cnv.width = cnv.height = size;
    const ctx = cnv.getContext('2d')!;
    const grad = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    grad.addColorStop(0, 'rgba(255,255,255,0.9)');
    grad.addColorStop(0.35, 'rgba(255,255,255,0.25)');
    grad.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, size, size);
    const texture = new T.CanvasTexture(cnv);
    this.glow = new T.Sprite(
      new T.SpriteMaterial({
        map: texture,
        transparent: true,
        blending: T.AdditiveBlending,
        depthWrite: false,
      }),
    );
    this.glow.scale.set(10, 10, 1);
    this.glow.position.copy(this.star.position);
    this.scene.add(this.glow);
  }

  /** Re-tints the whole scene from the live CSS custom properties. */
  applyThemeColors(): void {
    if (this.disposed) return;
    const T = this.three;
    const accent = this.cssColor('--accent', '#1FB6A6');
    const accentHover = this.cssColor('--accent-hover', '#3BD6C4');
    const bg = this.cssColor('--bg-primary', '#071629');
    // Light themes need normal blending — additive light disappears on white.
    const isLight = bg.getHSL({ h: 0, s: 0, l: 0 }).l > 0.6;

    this.renderer.setClearColor(bg, 1);
    this.scene.fog = new T.Fog(bg.getHex(), 40, 110);

    const starMat = this.stars.material as THREE.PointsMaterial;
    starMat.color = isLight
      ? new T.Color('#3A4654').lerp(accent, 0.3)
      : new T.Color('#ffffff').lerp(accent, 0.25);
    starMat.blending = isLight ? T.NormalBlending : T.AdditiveBlending;
    starMat.opacity = isLight ? 0.55 : 0.8;
    starMat.needsUpdate = true;

    for (const mat of this.ribbonMaterials) {
      (mat.uniforms['uColorA'].value as THREE.Color).copy(accent);
      (mat.uniforms['uColorB'].value as THREE.Color).copy(accentHover);
      mat.blending = isLight ? T.NormalBlending : T.AdditiveBlending;
      mat.needsUpdate = true;
    }

    (this.star.material as THREE.LineBasicMaterial).color.copy(accent);
    const glowMat = this.glow.material as THREE.SpriteMaterial;
    glowMat.color.copy(accentHover);
    glowMat.blending = isLight ? T.NormalBlending : T.AdditiveBlending;
    glowMat.opacity = isLight ? 0.35 : 1;
    glowMat.needsUpdate = true;

    if (this.reducedMotion) {
      this.renderer.render(this.scene, this.camera);
    }
  }

  private onPointerMove = (e: PointerEvent): void => {
    this.pointerX = (e.clientX / window.innerWidth) * 2 - 1;
    this.pointerY = (e.clientY / window.innerHeight) * 2 - 1;
  };

  private onResize(): void {
    if (this.disposed) return;
    const parent = this.canvas.parentElement ?? document.body;
    const width = parent.clientWidth || window.innerWidth;
    const height = parent.clientHeight || window.innerHeight;
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height, false);
    if (this.reducedMotion) {
      this.renderer.render(this.scene, this.camera);
    }
  }

  private loop = (): void => {
    if (this.disposed) return;
    this.rafId = requestAnimationFrame(this.loop);
    const t = (performance.now() - this.startTime) / 1000;

    for (const mat of this.ribbonMaterials) {
      mat.uniforms['uTime'].value = t;
    }
    this.stars.rotation.y = t * 0.004;
    this.star.rotation.x = t * 0.1;
    this.star.rotation.y = t * 0.14;

    // Lerped parallax (±0.6 units).
    this.camera.position.x += (this.pointerX * 0.6 - this.camera.position.x) * 0.04;
    this.camera.position.y += (2 - this.pointerY * 0.6 - this.camera.position.y) * 0.04;
    this.camera.lookAt(0, 6, -20);

    this.renderer.render(this.scene, this.camera);
  };

  destroy(): void {
    if (this.disposed) return;
    this.disposed = true;
    cancelAnimationFrame(this.rafId);
    window.removeEventListener('pointermove', this.onPointerMove);
    this.resizeObserver?.disconnect();
    this.themeObserver?.disconnect();

    this.scene.traverse((obj) => {
      const mesh = obj as THREE.Mesh;
      if (mesh.geometry) mesh.geometry.dispose();
      const material = (mesh as THREE.Mesh).material as
        | THREE.Material
        | THREE.Material[]
        | undefined;
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
