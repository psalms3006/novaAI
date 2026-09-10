/* mind/scene.js — Three.js scene setup: renderer, camera, lights, bloom, starfield */
import * as THREE from "three";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";

export function createScene(canvas) {
  // Renderer
  const renderer = new THREE.WebGLRenderer({
    canvas,
    antialias: true,
    alpha: false,
    powerPreference: "high-performance",
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(canvas.clientWidth, canvas.clientHeight, false);
  renderer.setClearColor(0x050709, 1);
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  // Scene
  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x050709, 0.004);

  // Camera
  const camera = new THREE.PerspectiveCamera(
    55,
    canvas.clientWidth / canvas.clientHeight,
    0.5,
    2000
  );
  camera.position.set(0, 120, 350);
  camera.lookAt(0, 0, 0);

  // Lights
  const ambientLight = new THREE.AmbientLight(0x1a2030, 0.8);
  scene.add(ambientLight);

  const pointLight = new THREE.PointLight(0x2dd4a8, 2.5, 800);
  pointLight.position.set(0, 80, 0);
  scene.add(pointLight);

  const rimLight = new THREE.DirectionalLight(0x38bdf8, 0.4);
  rimLight.position.set(-200, 100, -200);
  scene.add(rimLight);

  // Post-processing: bloom
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(
    new THREE.Vector2(canvas.clientWidth, canvas.clientHeight),
    0.6,   // strength
    0.5,   // radius
    0.7    // threshold
  );
  composer.addPass(bloom);

  // Starfield background
  const starCount = 2000;
  const starPositions = new Float32Array(starCount * 3);
  for (let i = 0; i < starCount; i++) {
    starPositions[i * 3] = (Math.random() - 0.5) * 2000;
    starPositions[i * 3 + 1] = (Math.random() - 0.5) * 2000;
    starPositions[i * 3 + 2] = (Math.random() - 0.5) * 2000;
  }
  const starGeometry = new THREE.BufferGeometry();
  starGeometry.setAttribute("position", new THREE.BufferAttribute(starPositions, 3));
  const starMaterial = new THREE.PointsMaterial({ color: 0x4e5b67, size: 0.8, sizeAttenuation: true });
  const stars = new THREE.Points(starGeometry, starMaterial);
  scene.add(stars);

  // Membrane (subtle wireframe sphere around core)
  const membraneGeo = new THREE.IcosahedronGeometry(140, 2);
  const membraneMat = new THREE.MeshBasicMaterial({
    color: 0x2dd4a8,
    wireframe: true,
    transparent: true,
    opacity: 0.04,
  });
  const membrane = new THREE.Mesh(membraneGeo, membraneMat);
  scene.add(membrane);

  return { renderer, scene, camera, composer, bloom, stars, membrane, pointLight };
}

export function resizeScene(ctx, width, height) {
  ctx.camera.aspect = width / height;
  ctx.camera.updateProjectionMatrix();
  ctx.renderer.setSize(width, height, false);
  ctx.composer.setSize(width, height);
}

export function renderScene(ctx) {
  ctx.membrane.rotation.y += 0.0003;
  ctx.membrane.rotation.x += 0.0001;
  ctx.stars.rotation.y += 0.00005;
  ctx.composer.render();
}
