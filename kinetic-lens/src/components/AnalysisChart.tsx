import { useId, useMemo } from "react";
import { scaleLinear } from "d3-scale";
import { line } from "d3-shape";
import {
  buildDynamicsChartRows,
  getDynamicsChartValues,
  dynamicsFrameAtIndex,
  hasDynamicsForMetric,
} from "../data/chartDynamics";
import { metricLabels, metricUnits, getMetricValues } from "../data/metrics";
import type { EstimatedDynamicsData, MetricKey, OpenSimMotion } from "../types";

type AnalysisChartProps = {
  data: OpenSimMotion;
  dynamics: EstimatedDynamicsData | null;
  duration: number;
  events: Array<{ time: number; label: string }>;
  frameIndex: number;
  metric: MetricKey;
  onMetricChange: (metric: MetricKey) => void;
  onSeek: (seconds: number) => void;
};

const width = 620;
const height = 420;
const margin = { top: 50, right: 92, bottom: 46, left: 48 };

export function AnalysisChart({
  data,
  dynamics,
  duration,
  events,
  frameIndex,
  metric,
  onMetricChange,
  onSeek,
}: AnalysisChartProps) {
  const titleId = useId();
  const current = getMetricValues(data.frames[frameIndex], metric);
  const currentDynamics = getDynamicsChartValues(
    dynamicsFrameAtIndex(dynamics, frameIndex),
    metric,
  );
  const showDynamics =
    Boolean(dynamics?.frames.length) && hasDynamicsForMetric(metric);

  const dynamicsRows = useMemo(() => {
    if (!dynamics?.frames.length || !hasDynamicsForMetric(metric)) return null;
    return buildDynamicsChartRows(dynamics, metric, data.frames.length);
  }, [data.frames.length, dynamics, metric]);

  const plot = useMemo(() => {
    const rows = data.frames.map((frame, index) => {
      const values = getMetricValues(frame, metric);
      const dynamicsRow = dynamicsRows?.[index];
      return {
        t: frame.time,
        left: values.left,
        right: values.right,
        leftForceN: dynamicsRow?.leftForceN ?? 0,
        rightForceN: dynamicsRow?.rightForceN ?? 0,
        leftMomentNm: dynamicsRow?.leftMomentNm ?? 0,
        rightMomentNm: dynamicsRow?.rightMomentNm ?? 0,
      };
    });

    const angleValues = rows.flatMap((row) => [row.left, row.right]);
    const angleLow = Math.min(...angleValues);
    const angleHigh = Math.max(...angleValues);
    const anglePadding = Math.max(8, (angleHigh - angleLow) * 0.16);

    const x = scaleLinear()
      .domain([0, duration])
      .range([margin.left, width - margin.right]);
    const yAngle = scaleLinear()
      .domain([Math.max(0, angleLow - anglePadding), angleHigh + anglePadding])
      .nice()
      .range([height - margin.bottom, margin.top]);

    const leftPath = line<(typeof rows)[number]>()
      .x((row) => x(row.t))
      .y((row) => yAngle(row.left))(rows);
    const rightPath = line<(typeof rows)[number]>()
      .x((row) => x(row.t))
      .y((row) => yAngle(row.right))(rows);

    let leftForcePath: string | null = null;
    let rightForcePath: string | null = null;
    let leftMomentPath: string | null = null;
    let rightMomentPath: string | null = null;
    let yForce = yAngle;
    let yMoment = yAngle;
    let forceTicks: number[] = [];
    let momentTicks: number[] = [];

    if (showDynamics && dynamicsRows) {
      const forceValues = rows.flatMap((row) => [row.leftForceN, row.rightForceN]);
      const momentValues = rows.flatMap((row) => [
        row.leftMomentNm,
        row.rightMomentNm,
      ]);
      const forceLow = Math.min(...forceValues);
      const forceHigh = Math.max(...forceValues);
      const forcePadding = Math.max(12, (forceHigh - forceLow) * 0.12);
      const momentLow = Math.min(...momentValues);
      const momentHigh = Math.max(...momentValues);
      const momentPadding = Math.max(4, (momentHigh - momentLow) * 0.12);

      yForce = scaleLinear()
        .domain([Math.max(0, forceLow - forcePadding), forceHigh + forcePadding])
        .nice()
        .range([height - margin.bottom, margin.top]);
      yMoment = scaleLinear()
        .domain([momentLow - momentPadding, momentHigh + momentPadding])
        .nice()
        .range([height - margin.bottom, margin.top]);

      leftForcePath = line<(typeof rows)[number]>()
        .x((row) => x(row.t))
        .y((row) => yForce(row.leftForceN))(rows);
      rightForcePath = line<(typeof rows)[number]>()
        .x((row) => x(row.t))
        .y((row) => yForce(row.rightForceN))(rows);
      leftMomentPath = line<(typeof rows)[number]>()
        .x((row) => x(row.t))
        .y((row) => yMoment(row.leftMomentNm))(rows);
      rightMomentPath = line<(typeof rows)[number]>()
        .x((row) => x(row.t))
        .y((row) => yMoment(row.rightMomentNm))(rows);
      forceTicks = yForce.ticks(4);
      momentTicks = yMoment.ticks(4);
    }

    return {
      rows,
      x,
      yAngle,
      yForce,
      yMoment,
      leftPath,
      rightPath,
      leftForcePath,
      rightForcePath,
      leftMomentPath,
      rightMomentPath,
      angleTicks: yAngle.ticks(4),
      forceTicks,
      momentTicks,
    };
  }, [data, duration, dynamicsRows, metric, showDynamics]);

  const currentTime = data.frames[frameIndex]?.time ?? 0;
  const cursorX = plot.x(currentTime);
  const metricOptions: MetricKey[] = ["knee", "hip", "trunk"];

  function handlePointer(event: React.PointerEvent<SVGSVGElement>) {
    if (event.buttons !== 1 && event.type !== "pointerdown") return;
    const bounds = event.currentTarget.getBoundingClientRect();
    const localX = ((event.clientX - bounds.left) / bounds.width) * width;
    const seconds = Math.min(duration, Math.max(0, plot.x.invert(localX)));
    onSeek(seconds);
  }

  return (
    <div className="chart-wrap">
      <div className="metric-tabs" aria-label="Joint angle metric">
        {metricOptions.map((option) => (
          <button
            key={option}
            className={metric === option ? "is-active" : ""}
            type="button"
            onClick={() => onMetricChange(option)}
          >
            {option === "knee" ? "Knee" : option === "hip" ? "Hip" : "Trunk"}
          </button>
        ))}
      </div>

      <div className="chart-readout">
        <div>
          <span className="chart-readout__eyebrow">Current frame</span>
          <strong>{metricLabels[metric]}</strong>
        </div>
        <div className="chart-readout__values">
          <span>
            <i className="series-dot series-dot--left" />
            L {current.left.toFixed(1)}°
          </span>
          {metric !== "trunk" && (
            <span>
              <i className="series-dot series-dot--right" />
              R {current.right.toFixed(1)}°
            </span>
          )}
          {showDynamics && currentDynamics && (
            <>
              <span>
                <i className="series-dot series-dot--force-left" />
                L {currentDynamics.leftForceN.toFixed(0)} N
              </span>
              <span>
                <i className="series-dot series-dot--force-right" />
                R {currentDynamics.rightForceN.toFixed(0)} N
              </span>
              <span>
                <i className="series-dot series-dot--moment-left" />
                L {currentDynamics.leftMomentNm.toFixed(1)} Nm
              </span>
              <span>
                <i className="series-dot series-dot--moment-right" />
                R {currentDynamics.rightMomentNm.toFixed(1)} Nm
              </span>
            </>
          )}
        </div>
      </div>

      {showDynamics && (
        <div className="chart-legend" aria-label="Chart series legend">
          <span>
            <i className="series-dot series-dot--left" />
            Angle L
          </span>
          <span>
            <i className="series-dot series-dot--right" />
            Angle R
          </span>
          <span>
            <i className="series-dot series-dot--force-left" />
            Force L
          </span>
          <span>
            <i className="series-dot series-dot--force-right" />
            Force R
          </span>
          <span>
            <i className="series-dot series-dot--moment-left" />
            Torque L
          </span>
          <span>
            <i className="series-dot series-dot--moment-right" />
            Torque R
          </span>
        </div>
      )}

      <svg
        className="angle-chart"
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-labelledby={titleId}
        onPointerDown={handlePointer}
        onPointerMove={handlePointer}
      >
        <title id={titleId}>
          {metricLabels[metric]} over the {duration.toFixed(2)} second clip
          {showDynamics ? " with estimated joint force and torque" : ""}
        </title>

        {plot.angleTicks.map((tick) => (
          <g key={`angle-${tick}`}>
            <line
              className="chart-gridline"
              x1={margin.left}
              x2={width - margin.right}
              y1={plot.yAngle(tick)}
              y2={plot.yAngle(tick)}
            />
            <text
              className="chart-axis-label chart-axis-label--angle"
              x={margin.left - 12}
              y={plot.yAngle(tick)}
              textAnchor="end"
              dominantBaseline="middle"
            >
              {tick}°
            </text>
          </g>
        ))}

        {showDynamics &&
          plot.forceTicks.map((tick) => (
            <text
              className="chart-axis-label chart-axis-label--force"
              key={`force-${tick}`}
              x={width - margin.right + 8}
              y={plot.yForce(tick)}
              textAnchor="start"
              dominantBaseline="middle"
            >
              {tick}N
            </text>
          ))}

        {showDynamics &&
          plot.momentTicks.map((tick) => (
            <text
              className="chart-axis-label chart-axis-label--moment"
              key={`moment-${tick}`}
              x={width - margin.right + 44}
              y={plot.yMoment(tick)}
              textAnchor="start"
              dominantBaseline="middle"
            >
              {tick}Nm
            </text>
          ))}

        {[0, 1, 2, 3].map((tick) => (
          <text
            className="chart-axis-label"
            key={tick}
            x={plot.x(Math.min(duration, tick))}
            y={height - 15}
            textAnchor={tick === 0 ? "start" : "middle"}
          >
            {Math.min(duration, tick).toFixed(1)}s
          </text>
        ))}

        {events.map((event) => (
          <g key={event.label}>
            <line
              className="chart-event-line"
              x1={plot.x(event.time)}
              x2={plot.x(event.time)}
              y1={margin.top}
              y2={height - margin.bottom}
            />
            <text
              className="chart-event-label"
              x={plot.x(event.time) + 5}
              y={margin.top + 12}
            >
              {event.label}
            </text>
          </g>
        ))}

        <path className="chart-line chart-line--left" d={plot.leftPath ?? ""} />
        {metric !== "trunk" && (
          <path
            className="chart-line chart-line--right"
            d={plot.rightPath ?? ""}
          />
        )}

        {showDynamics && (
          <>
            <path
              className="chart-line chart-line--force-left"
              d={plot.leftForcePath ?? ""}
            />
            <path
              className="chart-line chart-line--force-right"
              d={plot.rightForcePath ?? ""}
            />
            <path
              className="chart-line chart-line--moment-left"
              d={plot.leftMomentPath ?? ""}
            />
            <path
              className="chart-line chart-line--moment-right"
              d={plot.rightMomentPath ?? ""}
            />
          </>
        )}

        <line
          className="chart-cursor"
          x1={cursorX}
          x2={cursorX}
          y1={margin.top}
          y2={height - margin.bottom}
        />
        <circle
          className="chart-cursor-point chart-cursor-point--left"
          cx={cursorX}
          cy={plot.yAngle(current.left)}
          r="5"
        />
        {metric !== "trunk" && (
          <circle
            className="chart-cursor-point chart-cursor-point--right"
            cx={cursorX}
            cy={plot.yAngle(current.right)}
            r="5"
          />
        )}
        {showDynamics && currentDynamics && (
          <>
            <circle
              className="chart-cursor-point chart-cursor-point--force-left"
              cx={cursorX}
              cy={plot.yForce(currentDynamics.leftForceN)}
              r="4"
            />
            <circle
              className="chart-cursor-point chart-cursor-point--force-right"
              cx={cursorX}
              cy={plot.yForce(currentDynamics.rightForceN)}
              r="4"
            />
            <circle
              className="chart-cursor-point chart-cursor-point--moment-left"
              cx={cursorX}
              cy={plot.yMoment(currentDynamics.leftMomentNm)}
              r="4"
            />
            <circle
              className="chart-cursor-point chart-cursor-point--moment-right"
              cx={cursorX}
              cy={plot.yMoment(currentDynamics.rightMomentNm)}
              r="4"
            />
          </>
        )}

        <text
          className="chart-unit"
          x={margin.left}
          y={height - 15}
          textAnchor="start"
        >
          {metricUnits[metric]} · OpenSim IK
        </text>
        <text
          className="chart-unit chart-unit--estimated"
          x={width - margin.right}
          y={height - 15}
          textAnchor="end"
        >
          {showDynamics
            ? "Force N · Torque Nm · ESTIMATED GRF"
            : metric === "trunk"
              ? "Trunk has angle only · no ID estimate"
              : "Dynamics unavailable"}
        </text>
      </svg>
    </div>
  );
}
