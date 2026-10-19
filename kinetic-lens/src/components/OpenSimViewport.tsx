import {
  Component,
  Suspense,
  useEffect,
  useMemo,
  useState,
  type ErrorInfo,
  type ReactNode,
} from "react";
import { Canvas, useThree } from "@react-three/fiber";
import {
  Environment,
  Grid,
  Html,
  Line,
  OrbitControls,
  useCursor,
  useGLTF,
} from "@react-three/drei";
import {
  Color,
  Mesh,
  MeshPhysicalMaterial,
  Quaternion,
  Vector3,
} from "three";
import {
  ArrowUpRight,
  Bone,
  CircleGauge,
  Focus,
  Minus,
  Plus,
  RotateCcw,
  ScanLine,
} from "lucide-react";
import {
  confidenceLabel,
  directionText,
  DYNAMICS_JOINT_LABELS,
  dynamicsTextAlternative,
  radiansToDegrees,
  unitDirection,
} from "../data/dynamics";
import {
  deriveJointMechanics,
  mechanicsTextAlternative,
  type JointMechanic,
} from "../data/mechanics";
import type {
  DynamicsJointId,
  EstimatedDynamicsData,
  EstimatedDynamicsFrame,
  OpenSimFrame,
  OpenSimMotion,
  Vec3,
} from "../types";

type CameraPreset = "three-quarter" | "front" | "side";

type OpenSimViewportProps = {
  motion: OpenSimMotion;
  dynamics: EstimatedDynamicsData | null;
  frameIndex: number;
};

function LoadingScene() {
  return (
    <div className="model-loading" role="status">
      <span className="model-loading__orb" />
      <strong>Loading official OpenSim geometry</strong>
      <small>Rajagopal · bones + 80 musculotendon paths</small>
    </div>
  );
}

class ModelErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("OpenSim viewport failed", error, info);
  }

  render() {
    if (this.state.failed) {
      return (
        <div className="model-fallback" role="status">
          <Bone aria-hidden="true" />
          <strong>3D renderer unavailable</strong>
          <span>The synchronized curves and source video remain usable.</span>
        </div>
      );
    }

    return this.props.children;
  }
}

function CameraRig({ preset }: { preset: CameraPreset }) {
  const { camera } = useThree();
  const target = useMemo(() => new Vector3(0, 0.98, 0), []);

  useEffect(() => {
    const positions: Record<CameraPreset, [number, number, number]> = {
      "three-quarter": [2.25, 1.45, 2.75],
      front: [0, 1.25, 3.65],
      side: [3.65, 1.25, 0],
    };

    camera.position.set(...positions[preset]);
    camera.lookAt(target);
    camera.updateProjectionMatrix();
  }, [camera, preset, target]);

  return (
    <OrbitControls
      key={preset}
      makeDefault
      target={target}
      minDistance={1.7}
      maxDistance={6}
      minPolarAngle={Math.PI * 0.18}
      maxPolarAngle={Math.PI * 0.82}
      enablePan={false}
      dampingFactor={0.08}
      enableDamping
    />
  );
}

function BoneModel({
  frame,
  bodyNames,
  showBones,
  onReady,
}: {
  frame: OpenSimFrame;
  bodyNames: string[];
  showBones: boolean;
  onReady: () => void;
}) {
  const gltf = useGLTF("/assets/model/rajagopal-bones.glb");
  const model = useMemo(() => gltf.scene.clone(true), [gltf.scene]);

  useEffect(() => {
    const boneMaterial = new MeshPhysicalMaterial({
      color: new Color("#f3f5ee"),
      roughness: 0.62,
      metalness: 0.02,
      clearcoat: 0.08,
    });

    model.traverse((object) => {
      if (object instanceof Mesh) {
        object.material = boneMaterial;
        object.castShadow = true;
        object.receiveShadow = true;
      }
    });

    onReady();
    return () => boneMaterial.dispose();
  }, [model, onReady]);

  useEffect(() => {
    bodyNames.forEach((bodyName) => {
      const object = model.getObjectByName(bodyName);
      const transform = frame.bodies[bodyName];
      if (!object || !transform) return;
      object.position.set(...transform.p);
      object.quaternion.set(...transform.q);
      object.updateMatrix();
    });
  }, [bodyNames, frame, model]);

  model.visible = showBones;
  return <primitive object={model} />;
}

function MusclePaths({
  frame,
  visible,
}: {
  frame: OpenSimFrame;
  visible: boolean;
}) {
  if (!visible) return null;

  return (
    <group>
      {frame.muscles.map((muscle) => (
        <Line
          key={muscle.name}
          points={muscle.points}
          color="#e07a6a"
          lineWidth={1.05}
          transparent
          opacity={0.78}
          depthWrite={false}
        />
      ))}
    </group>
  );
}

function DirectionArrow({
  origin,
  direction,
  length,
  color,
  shaftRadius = 0.006,
}: {
  origin: [number, number, number];
  direction: [number, number, number];
  length: number;
  color: string;
  shaftRadius?: number;
}) {
  const geometry = useMemo(() => {
    const normalized = new Vector3(...direction).normalize();
    const quaternion = new Quaternion().setFromUnitVectors(
      new Vector3(0, 1, 0),
      normalized,
    );
    const headLength = Math.min(length * 0.32, 0.055);
    const shaftLength = Math.max(0.01, length - headLength);
    return {
      quaternion,
      shaftLength,
      headLength,
      shaftPosition: normalized.clone().multiplyScalar(shaftLength * 0.5),
      headPosition: normalized
        .clone()
        .multiplyScalar(shaftLength + headLength * 0.5),
    };
  }, [direction, length]);

  return (
    <group position={origin}>
      <mesh
        position={geometry.shaftPosition}
        quaternion={geometry.quaternion}
        renderOrder={9}
      >
        <cylinderGeometry
          args={[shaftRadius, shaftRadius, geometry.shaftLength, 8]}
        />
        <meshBasicMaterial color={color} depthTest={false} />
      </mesh>
      <mesh
        position={geometry.headPosition}
        quaternion={geometry.quaternion}
        renderOrder={9}
      >
        <coneGeometry
          args={[shaftRadius * 2.7, geometry.headLength, 10]}
        />
        <meshBasicMaterial color={color} depthTest={false} />
      </mesh>
    </group>
  );
}

function RotationArc({ joint }: { joint: JointMechanic }) {
  const geometry = useMemo(() => {
    const radius = 0.065;
    const start = joint.rotationDirection > 0 ? -1.05 : 1.05;
    const sweep = joint.rotationDirection * 1.75;
    const points: [number, number, number][] = [];
    for (let index = 0; index <= 18; index += 1) {
      const angle = start + sweep * (index / 18);
      points.push([
        joint.position[0] + Math.cos(angle) * radius,
        joint.position[1] + Math.sin(angle) * radius,
        joint.position[2] + 0.018,
      ]);
    }
    const endAngle = start + sweep;
    const tangent: [number, number, number] = [
      -Math.sin(endAngle) * joint.rotationDirection,
      Math.cos(endAngle) * joint.rotationDirection,
      0,
    ];
    return { points, tangent, end: points.at(-1)! };
  }, [joint]);

  return (
    <>
      <Line
        points={geometry.points}
        color="#a8b88a"
        lineWidth={1.4}
        depthTest={false}
        renderOrder={8}
      />
      <DirectionArrow
        origin={geometry.end}
        direction={geometry.tangent}
        length={0.032}
        color="#a8b88a"
        shaftRadius={0.004}
      />
    </>
  );
}

function MechanicsLayer({
  frame,
  mechanics,
  visible,
}: {
  frame: OpenSimFrame;
  mechanics: JointMechanic[];
  visible: boolean;
}) {
  if (!visible) return null;

  return (
    <group>
      {mechanics.map((joint) => {
        const arrowLength = Math.min(0.31, 0.08 + joint.speedMps * 0.055);
        const labelOffset = joint.id.endsWith("-l") ? -0.065 : 0.065;
        return (
          <group key={joint.id}>
            <mesh position={joint.position} renderOrder={10}>
              <sphereGeometry args={[0.022, 12, 12]} />
              <meshBasicMaterial color="#e1fe67" depthTest={false} />
            </mesh>
            <DirectionArrow
              origin={joint.position}
              direction={joint.direction}
              length={arrowLength}
              color="#e1fe67"
            />
            <RotationArc joint={joint} />
            <Html
              position={[
                joint.position[0] + labelOffset,
                joint.position[1] + 0.06,
                joint.position[2] + 0.025,
              ]}
              center
              zIndexRange={[7, 0]}
            >
              <div className="joint-mechanics-label">
                <span>{joint.shortLabel}</span>
                <b>{joint.speedMps.toFixed(1)} m/s</b>
              </div>
            </Html>
          </group>
        );
      })}

      <mesh position={frame.centerOfMass} renderOrder={10}>
        <sphereGeometry args={[0.03, 14, 14]} />
        <meshBasicMaterial color="#e1fe67" depthTest={false} />
      </mesh>
      <DirectionArrow
        origin={frame.centerOfMass}
        direction={[0, -1, 0]}
        length={0.34}
        color="#e1fe67"
        shaftRadius={0.008}
      />
      <Html
        position={[
          frame.centerOfMass[0] + 0.09,
          frame.centerOfMass[1] - 0.17,
          frame.centerOfMass[2] + 0.025,
        ]}
        center
        zIndexRange={[7, 0]}
      >
        <div className="joint-mechanics-label joint-mechanics-label--gravity">
          <span>COM · g</span>
          <b>gravity</b>
        </div>
      </Html>
    </group>
  );
}

function JointHitTarget({
  id,
  position,
  selected,
  onSelect,
}: {
  id: DynamicsJointId;
  position: Vec3;
  selected: boolean;
  onSelect: (id: DynamicsJointId) => void;
}) {
  const [hovered, setHovered] = useState(false);
  useCursor(hovered);

  return (
    <group position={position}>
      <mesh
        renderOrder={14}
        onClick={(event) => {
          event.stopPropagation();
          onSelect(id);
        }}
        onPointerOver={(event) => {
          event.stopPropagation();
          setHovered(true);
        }}
        onPointerOut={() => setHovered(false)}
      >
        <sphereGeometry args={[0.065, 16, 16]} />
        <meshBasicMaterial transparent opacity={0} depthWrite={false} />
      </mesh>
      <mesh renderOrder={12}>
        <torusGeometry args={[selected ? 0.047 : 0.034, 0.006, 10, 28]} />
        <meshBasicMaterial
          color={selected ? "#a8b88a" : "#e8c26a"}
          transparent
          opacity={selected ? 1 : 0.54}
          depthTest={false}
        />
      </mesh>
    </group>
  );
}

function EstimatedDynamicsLayer({
  frame,
  bodyWeightN,
  selectedJoint,
  onSelectJoint,
}: {
  frame: EstimatedDynamicsFrame;
  bodyWeightN: number;
  selectedJoint: DynamicsJointId;
  onSelectJoint: (id: DynamicsJointId) => void;
}) {
  const selected = frame.joints[selectedJoint];
  const selectedDirection = unitDirection(selected.force);
  const selectedArrowLength = Math.min(
    0.58,
    0.16 + selected.bodyWeight * 0.27,
  );

  return (
    <group>
      {(["left", "right"] as const).map((side) => {
        const force = frame.grf[side];
        if (!force.active) return null;
        const magnitudeN = Math.hypot(...force.force);
        const bodyWeight = magnitudeN / bodyWeightN;
        return (
          <group key={side}>
            <mesh position={force.point} renderOrder={11}>
              <cylinderGeometry args={[0.055, 0.055, 0.006, 24]} />
              <meshBasicMaterial
                color="#e8c26a"
                transparent
                opacity={0.7}
                depthTest={false}
              />
            </mesh>
            <DirectionArrow
              origin={force.point}
              direction={unitDirection(force.force)}
              length={Math.min(0.7, 0.18 + bodyWeight * 0.32)}
              color="#e8c26a"
              shaftRadius={0.01}
            />
            <Html
              position={[
                force.point[0] + (side === "left" ? -0.1 : 0.1),
                force.point[1] + 0.28,
                force.point[2],
              ]}
              center
              zIndexRange={[8, 0]}
            >
              <div className="force-label force-label--grf">
                <span>EST. {side === "left" ? "L" : "R"} GRF</span>
                <b>{magnitudeN.toFixed(0)} N · {bodyWeight.toFixed(2)} BW</b>
              </div>
            </Html>
          </group>
        );
      })}

      {(Object.keys(frame.joints) as DynamicsJointId[]).map((id) => (
        <JointHitTarget
          key={id}
          id={id}
          position={frame.joints[id].position}
          selected={id === selectedJoint}
          onSelect={onSelectJoint}
        />
      ))}

      <DirectionArrow
        origin={selected.position}
        direction={selectedDirection}
        length={selectedArrowLength}
        color="#a8b88a"
        shaftRadius={0.011}
      />
      <Html
        position={[
          selected.position[0] + (selectedJoint.endsWith("_l") ? -0.12 : 0.12),
          selected.position[1] + 0.11,
          selected.position[2] + 0.02,
        ]}
        center
        zIndexRange={[9, 0]}
      >
        <div className="force-label force-label--reaction">
          <span>EST. JRF · {DYNAMICS_JOINT_LABELS[selectedJoint]}</span>
          <b>{selected.magnitudeN.toFixed(0)} N · {selected.bodyWeight.toFixed(2)} BW</b>
        </div>
      </Html>
    </group>
  );
}

function Scene({
  frame,
  frameIndex,
  motion,
  dynamicsFrame,
  bodyWeightN,
  selectedJoint,
  onSelectJoint,
  showBones,
  showMuscles,
  showMechanics,
  cameraPreset,
  onReady,
}: {
  frame: OpenSimFrame;
  frameIndex: number;
  motion: OpenSimMotion;
  dynamicsFrame: EstimatedDynamicsFrame | null;
  bodyWeightN: number;
  selectedJoint: DynamicsJointId;
  onSelectJoint: (id: DynamicsJointId) => void;
  showBones: boolean;
  showMuscles: boolean;
  showMechanics: boolean;
  cameraPreset: CameraPreset;
  onReady: () => void;
}) {
  const centerX = frame.pelvis[0];
  const mechanics = useMemo(
    () => deriveJointMechanics(motion, frameIndex),
    [frameIndex, motion],
  );

  return (
    <>
      <color attach="background" args={["#0c0e0c"]} />
      <ambientLight intensity={1.35} />
      <directionalLight
        position={[3.5, 5.5, 4]}
        intensity={3.2}
        castShadow
        shadow-mapSize-width={1024}
        shadow-mapSize-height={1024}
      />
      <directionalLight position={[-3, 2, -2]} intensity={0.9} color="#e1fe67" />

      <group position={[-centerX, 0, 0]}>
        <BoneModel
          frame={frame}
          bodyNames={motion.bodyNames}
          showBones={showBones}
          onReady={onReady}
        />
        <MusclePaths frame={frame} visible={showMuscles} />
        <MechanicsLayer
          frame={frame}
          mechanics={mechanics}
          visible={showMechanics}
        />
        {showMechanics && dynamicsFrame && (
          <EstimatedDynamicsLayer
            frame={dynamicsFrame}
            bodyWeightN={bodyWeightN}
            selectedJoint={selectedJoint}
            onSelectJoint={onSelectJoint}
          />
        )}
      </group>

      <Grid
        position={[0, -0.01, 0]}
        args={[8, 8]}
        cellSize={0.25}
        cellThickness={0.38}
        cellColor="#2a2e26"
        sectionSize={1}
        sectionThickness={0.8}
        sectionColor="#4a5240"
        fadeDistance={5.2}
        fadeStrength={1.8}
        infiniteGrid
      />
      {/* Local HDR — drei's CDN (raw.githack.com) often times out and leaves the viewport stuck loading. */}
      <Environment files="/assets/hdri/empty_warehouse_01_1k.hdr" />
      <CameraRig preset={cameraPreset} />
    </>
  );
}

export function OpenSimViewport({
  motion,
  dynamics,
  frameIndex,
}: OpenSimViewportProps) {
  const [showBones, setShowBones] = useState(true);
  const [showMuscles, setShowMuscles] = useState(false);
  const [showMechanics, setShowMechanics] = useState(true);
  const [cameraPreset, setCameraPreset] =
    useState<CameraPreset>("front");
  const [selectedJoint, setSelectedJoint] =
    useState<DynamicsJointId>("knee_r");
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);
  const [ready, setReady] = useState(false);
  const frame = motion.frames[Math.min(frameIndex, motion.frames.length - 1)];
  const dynamicsFrame =
    dynamics && dynamics.frames.length > 0
      ? dynamics.frames[Math.min(frameIndex, dynamics.frames.length - 1)]
      : null;
  const selectedDynamics = dynamicsFrame?.joints[selectedJoint];
  const mechanics = useMemo(
    () => deriveJointMechanics(motion, frameIndex),
    [frameIndex, motion],
  );
  const mechanicsSummary = useMemo(
    () => mechanicsTextAlternative(mechanics),
    [mechanics],
  );
  const boundedDpr =
    typeof window !== "undefined" && window.innerWidth < 720
      ? [1, 1.25]
      : [1, 1.75];

  return (
    <div
      className="model-viewport"
      data-model-ready={ready ? "true" : "false"}
    >
      {!ready && <LoadingScene />}
      <ModelErrorBoundary>
        <Suspense fallback={null}>
          <Canvas
            dpr={boundedDpr as [number, number]}
            camera={{ position: [0, 1.25, 3.65], fov: 34, near: 0.05, far: 80 }}
            shadows
            gl={{ antialias: true, alpha: false, powerPreference: "high-performance" }}
          >
            <Scene
              frame={frame}
              frameIndex={frameIndex}
              motion={motion}
              dynamicsFrame={dynamicsFrame}
              bodyWeightN={dynamics?.metadata.bodyWeightN ?? 912.0}
              selectedJoint={selectedJoint}
              onSelectJoint={setSelectedJoint}
              showBones={showBones}
              showMuscles={showMuscles}
              showMechanics={showMechanics}
              cameraPreset={cameraPreset}
              onReady={() => setReady(true)}
            />
          </Canvas>
        </Suspense>
      </ModelErrorBoundary>

      <div className="model-toolbar model-toolbar--top" aria-label="Model layers">
        <button
          className={showBones ? "is-active" : ""}
          type="button"
          onClick={() => setShowBones((value) => !value)}
          aria-pressed={showBones}
        >
          <Bone size={14} />
          Bones
        </button>
        <button
          className={showMuscles ? "is-active model-muscle-toggle" : ""}
          type="button"
          onClick={() => setShowMuscles((value) => !value)}
          aria-pressed={showMuscles}
        >
          <ScanLine size={14} />
          Muscles
        </button>
        <button
          className={showMechanics ? "is-active model-mechanics-toggle" : ""}
          type="button"
          onClick={() => setShowMechanics((value) => !value)}
          aria-pressed={showMechanics}
        >
          <ArrowUpRight size={14} />
          Mechanics
        </button>
      </div>

      {showMechanics && (
        <div className="mechanics-key" aria-label="Mechanics overlay key">
          <strong>Mechanics · current frame</strong>
          <span>
            <i className="mechanics-key__velocity" aria-hidden="true" />
            Joint-centre velocity
          </span>
          <span>
            <i className="mechanics-key__rotation" aria-hidden="true" />
            Flex / extend direction
          </span>
          <span>
            <i className="mechanics-key__gravity" aria-hidden="true" />
            Gravity direction at COM
          </span>
          <span>
            <i className="mechanics-key__grf" aria-hidden="true" />
            ESTIMATED GRF
          </span>
          <span>
            <i className="mechanics-key__reaction" aria-hidden="true" />
            Selected joint reaction
          </span>
          <small>OpenSim ID + Joint Reaction · not measured</small>
        </div>
      )}
      <p className="sr-only" aria-live="polite">
        {showMechanics
          ? `${mechanicsSummary}${
              selectedDynamics
                ? ` ${dynamicsTextAlternative(selectedJoint, selectedDynamics)}`
                : ""
            }`
          : "Mechanics overlay is hidden."}
      </p>

      {showMechanics && dynamics && dynamicsFrame && selectedDynamics && (
        <section
          className={`dynamics-inspector${
            inspectorCollapsed ? " is-collapsed" : ""
          }`}
          aria-label="Estimated joint dynamics"
        >
          <header>
            <div>
              <span className="estimated-badge">ESTIMATED GRF</span>
              <strong>{DYNAMICS_JOINT_LABELS[selectedJoint]}</strong>
            </div>
            <div className="dynamics-inspector__header-actions">
              <small>{dynamicsFrame.contact.phase}</small>
              <button
                type="button"
                onClick={() => setInspectorCollapsed((value) => !value)}
                aria-expanded={!inspectorCollapsed}
                aria-label={
                  inspectorCollapsed
                    ? "Show estimated joint dynamics"
                    : "Hide estimated joint dynamics"
                }
                title={
                  inspectorCollapsed
                    ? "Show dynamics panel"
                    : "Hide dynamics panel"
                }
              >
                {inspectorCollapsed ? (
                  <Plus size={14} aria-hidden="true" />
                ) : (
                  <Minus size={14} aria-hidden="true" />
                )}
              </button>
            </div>
          </header>

          {!inspectorCollapsed && (
            <div className="dynamics-inspector__body">
              <div className="joint-selector" aria-label="Select joint">
                {dynamics.jointIds.map((jointId) => (
                  <button
                    key={jointId}
                    type="button"
                    className={selectedJoint === jointId ? "is-active" : ""}
                    aria-pressed={selectedJoint === jointId}
                    onClick={() => setSelectedJoint(jointId)}
                  >
                    {DYNAMICS_JOINT_LABELS[jointId]
                      .replace("Right ", "R ")
                      .replace("Left ", "L ")}
                  </button>
                ))}
              </div>

              <div className="dynamics-readout">
                <div className="dynamics-readout__primary">
                  <span>Joint reaction force</span>
                  <strong>{selectedDynamics.magnitudeN.toFixed(0)} <small>N</small></strong>
                  <b>{selectedDynamics.bodyWeight.toFixed(2)} BW</b>
                </div>
                <dl>
                  <div>
                    <dt>Joint moment</dt>
                    <dd>{selectedDynamics.flexionMomentNm.toFixed(1)} Nm</dd>
                  </div>
                  <div>
                    <dt>Flex / extend</dt>
                    <dd>{radiansToDegrees(selectedDynamics.angleRad).toFixed(1)}°</dd>
                  </div>
                  <div>
                    <dt>Angular velocity</dt>
                    <dd>
                      {radiansToDegrees(
                        selectedDynamics.angularVelocityRadS,
                      ).toFixed(1)}°/s
                    </dd>
                  </div>
                  <div>
                    <dt>Confidence</dt>
                    <dd>
                      {Math.round(selectedDynamics.confidence * 100)}% ·{" "}
                      {confidenceLabel(selectedDynamics.confidence)}
                    </dd>
                  </div>
                </dl>
              </div>

              <div className="force-direction">
                <span>Reaction direction · ground frame</span>
                <code>{directionText(selectedDynamics.force)}</code>
              </div>
              <p>
                Conditional OpenSim result from estimated loads. Not a measured or
                clinical force.
              </p>
            </div>
          )}
        </section>
      )}

      <div className="model-toolbar model-toolbar--bottom" aria-label="Camera view">
        <button
          className={cameraPreset === "front" ? "is-active" : ""}
          type="button"
          onClick={() => setCameraPreset("front")}
          aria-label="Front view"
        >
          <Focus size={15} />
          Front
        </button>
        <button
          className={cameraPreset === "side" ? "is-active" : ""}
          type="button"
          onClick={() => setCameraPreset("side")}
          aria-label="Side view"
        >
          <CircleGauge size={15} />
          Side
        </button>
        <button
          type="button"
          onClick={() => {
            setCameraPreset("front");
          }}
          aria-label="Reset 3D view"
        >
          <RotateCcw size={15} />
          Reset
        </button>
      </div>

      <div className="orientation-cue" aria-hidden="true">
        <span className="orientation-cue__y">Y</span>
        <span className="orientation-cue__x">X</span>
        <span className="orientation-cue__z">Z</span>
      </div>
    </div>
  );
}

useGLTF.preload("/assets/model/rajagopal-bones.glb");
