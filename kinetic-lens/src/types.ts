export type Vec3 = [number, number, number];
export type Quaternion = [number, number, number, number];

export type ApparentAngles = {
  leftKnee: number;
  rightKnee: number;
  leftHip: number;
  rightHip: number;
  trunkLean: number;
};

export type PoseFrame = {
  keypoints: Array<[number, number, number]>;
  angles: ApparentAngles;
};

export type MotionData = {
  metadata: {
    source: string;
    subjectHeightCm: number;
    subjectMassKg: number;
    frameCount: number;
    nominalFps: number;
    durationSeconds: number;
    status: string;
  };
  events: Array<{ time: number; label: string }>;
  frames: PoseFrame[];
};

export type BodyTransform = {
  p: Vec3;
  q: Quaternion;
};

export type MusclePath = {
  name: string;
  points: Vec3[];
};

export type OpenSimFrame = {
  time: number;
  pelvis: Vec3;
  centerOfMass: Vec3;
  bodies: Record<string, BodyTransform>;
  muscles: MusclePath[];
  coordinates: Record<string, number>;
  markerRmseM: number;
};

export type OpenSimMotion = {
  metadata: {
    modelName: string;
    modelVersion: string;
    modelSource: string;
    modelLicense: string;
    bodyCount: number;
    muscleCount: number;
    defaultModelMassKg: number;
    subjectHeightCm: number;
    subjectMassKg: number;
    visualScale: number;
    pipeline: string;
    solver: string;
    meanMarkerRmseM: number;
    maxMarkerErrorM: number;
    warning: string;
  };
  bodyNames: string[];
  frames: OpenSimFrame[];
};

export type DynamicsJointId =
  | "hip_r"
  | "knee_r"
  | "ankle_r"
  | "hip_l"
  | "knee_l"
  | "ankle_l";

export type EstimatedForce = {
  active: boolean;
  weight: number;
  force: Vec3;
  point: Vec3;
};

export type DynamicsJointFrame = {
  position: Vec3;
  force: Vec3;
  magnitudeN: number;
  bodyWeight: number;
  reactionMoment: Vec3;
  momentMagnitudeNm: number;
  flexionMomentNm: number;
  angleRad: number;
  angularVelocityRadS: number;
  confidence: number;
};

export type EstimatedDynamicsFrame = {
  time: number;
  contact: {
    phase: string;
    left: boolean;
    right: boolean;
  };
  grf: {
    total: {
      force: Vec3;
      magnitudeN: number;
      bodyWeight: number;
    };
    left: EstimatedForce;
    right: EstimatedForce;
  };
  joints: Record<DynamicsJointId, DynamicsJointFrame>;
};

export type EstimatedDynamicsData = {
  metadata: {
    status: "ESTIMATED GRF";
    subjectMassKg: number;
    bodyWeightN: number;
    source: string;
    solver: string;
    comFilter: string;
    kinematicsLowpassHz: number;
    confidenceCeiling: number;
    warning: string;
  };
  jointIds: DynamicsJointId[];
  frames: EstimatedDynamicsFrame[];
};

export type MetricKey = "knee" | "hip" | "trunk";
