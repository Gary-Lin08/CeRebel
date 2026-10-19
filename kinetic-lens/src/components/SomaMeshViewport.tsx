import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { Grid, OrbitControls } from "@react-three/drei";
import {
  BufferAttribute,
  BufferGeometry,
  Color,
  DoubleSide,
  MeshStandardMaterial,
} from "three";

export type SomaMeshSequence = {
  faces: Uint32Array;
  verts: Float32Array;
  frameCount: number;
  vertexCount: number;
  faceCount: number;
  fps: number;
  space: "incam" | "global";
};

const HEADER_BYTES = 28; // 4s + 4×u32 + f32 + u32 = 4+16+4+4 = 28

function decodeFloat16(u16: number): number {
  const sign = (u16 & 0x8000) >> 15;
  const exp = (u16 & 0x7c00) >> 10;
  const frac = u16 & 0x03ff;
  if (exp === 0) {
    return (sign ? -1 : 1) * (2 ** -14) * (frac / 1024);
  }
  if (exp === 0x1f) {
    return frac ? Number.NaN : (sign ? -1 : 1) * Number.POSITIVE_INFINITY;
  }
  return (sign ? -1 : 1) * (2 ** (exp - 15)) * (1 + frac / 1024);
}

export async function loadSomaMeshSequence(
  url = "/assets/soma-mesh.bin",
): Promise<SomaMeshSequence> {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`SOMA mesh unavailable (${response.status})`);
  }
  const buffer = await response.arrayBuffer();
  const view = new DataView(buffer);
  const magic =
    String.fromCharCode(view.getUint8(0)) +
    String.fromCharCode(view.getUint8(1)) +
    String.fromCharCode(view.getUint8(2)) +
    String.fromCharCode(view.getUint8(3));
  if (magic !== "SOMA" && magic !== "SOMA1") {
    throw new Error(`Unexpected SOMA mesh magic: ${magic}`);
  }
  const version = view.getUint32(4, true);
  if (version !== 1 && version !== 2) {
    throw new Error(`Unsupported SOMA mesh version: ${version}`);
  }
  const faceCount = view.getUint32(8, true);
  const vertexCount = view.getUint32(12, true);
  const frameCount = view.getUint32(16, true);
  const fps = view.getFloat32(20, true);
  const spaceId = view.getUint32(24, true);

  const facesOffset = HEADER_BYTES;
  const facesBytes = faceCount * 3 * 4;
  const vertsOffset = facesOffset + facesBytes;
  const faces = new Uint32Array(buffer, facesOffset, faceCount * 3);
  const floatCount = frameCount * vertexCount * 3;
  let verts: Float32Array;
  if (version === 2) {
    verts = new Float32Array(floatCount);
    for (let i = 0; i < floatCount; i += 1) {
      verts[i] = decodeFloat16(view.getUint16(vertsOffset + i * 2, true));
    }
  } else {
    verts = new Float32Array(buffer, vertsOffset, floatCount);
  }

  return {
    faces,
    verts,
    frameCount,
    vertexCount,
    faceCount,
    fps: fps || 30,
    space: spaceId === 1 ? "incam" : "global",
  };
}

type SomaMeshViewportProps = {
  mesh: SomaMeshSequence | null;
  frameIndex: number;
  failed?: boolean;
};

function SomaBody({
  mesh,
  frameIndex,
}: {
  mesh: SomaMeshSequence;
  frameIndex: number;
}) {
  const geometry = useMemo(() => {
    const geo = new BufferGeometry();
    const positions = new Float32Array(mesh.vertexCount * 3);
    geo.setAttribute("position", new BufferAttribute(positions, 3));
    geo.setIndex(Array.from(mesh.faces));
    return geo;
  }, [mesh]);

  const material = useMemo(
    () =>
      new MeshStandardMaterial({
        color: new Color("#5aa8d4"),
        roughness: 0.45,
        metalness: 0.08,
        side: DoubleSide,
        flatShading: true,
      }),
    [],
  );

  const smoothFrame = useRef(frameIndex);
  const scratch = useMemo(
    () => new Float32Array(mesh.vertexCount * 3),
    [mesh.vertexCount],
  );

  useEffect(() => {
    smoothFrame.current = frameIndex;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only reset on mesh swap
  }, [mesh]);

  useEffect(() => () => {
    geometry.dispose();
    material.dispose();
  }, [geometry, material]);

  useFrame((_, delta) => {
    const maxF = Math.max(mesh.frameCount - 1, 0);
    const target = Math.min(maxF, Math.max(0, frameIndex));
    const follow = 1 - Math.exp(-8 * Math.min(delta, 0.05));
    smoothFrame.current += (target - smoothFrame.current) * follow;

    const f = smoothFrame.current;
    const i0 = Math.min(maxF, Math.max(0, Math.floor(f)));
    const i1 = Math.min(maxF, i0 + 1);
    const t = f - i0;
    const stride = mesh.vertexCount * 3;
    const src = mesh.verts;
    const aBase = i0 * stride;
    const bBase = i1 * stride;
    const out = scratch;

    if (t <= 0.001 || i0 === i1) {
      out.set(src.subarray(aBase, aBase + stride));
    } else if (t >= 0.999) {
      out.set(src.subarray(bBase, bBase + stride));
    } else {
      for (let i = 0; i < stride; i += 1) {
        const a = src[aBase + i];
        out[i] = a + (src[bBase + i] - a) * t;
      }
    }

    const attr = geometry.getAttribute("position") as BufferAttribute;
    (attr.array as Float32Array).set(out);
    attr.needsUpdate = true;
  });

  return <mesh geometry={geometry} material={material} />;
}

function LoadingMesh() {
  return (
    <div className="model-loading" role="status">
      <span className="model-loading__orb" />
      <strong>Loading egocentric SOMA stream</strong>
      <small>GEM-X body mesh · camera-aligned</small>
    </div>
  );
}

export function SomaMeshViewport({
  mesh,
  frameIndex,
  failed = false,
}: SomaMeshViewportProps) {
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (mesh) setReady(true);
  }, [mesh]);

  if (failed) {
    return (
      <div className="model-fallback" role="status">
        <strong>Egocentric mesh unavailable</strong>
        <span>Re-run the session with --fake-egocentric so SOMA display packs are exported.</span>
      </div>
    );
  }

  if (!mesh || !ready) {
    return <LoadingMesh />;
  }

  return (
    <div className="soma-stage">
      <Canvas
        camera={{ position: [0, 1.0, 3.0], fov: 40, near: 0.05, far: 80 }}
        gl={{ antialias: true }}
        dpr={[1, 1.75]}
      >
        <color attach="background" args={["#060709"]} />
        <ambientLight intensity={0.55} />
        <directionalLight position={[2.5, 4, 2]} intensity={1.15} />
        <directionalLight position={[-2, 1.5, -1]} intensity={0.35} />
        <Suspense fallback={null}>
          <SomaBody mesh={mesh} frameIndex={frameIndex} />
          <Grid
            position={[0, 0.001, 0]}
            args={[8, 8]}
            cellSize={0.25}
            cellThickness={0.6}
            sectionSize={1}
            sectionThickness={1.1}
            fadeDistance={10}
            fadeStrength={1.4}
            cellColor="#1c2430"
            sectionColor="#2a3648"
          />
          <OrbitControls
            enablePan
            enableDamping
            dampingFactor={0.08}
            target={[0, 0.9, 0]}
            minDistance={0.8}
            maxDistance={8}
          />
        </Suspense>
      </Canvas>
    </div>
  );
}
