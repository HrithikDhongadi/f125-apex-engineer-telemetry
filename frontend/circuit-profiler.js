import Konva from "konva";

const COLORS = {
  trace: "#65e6ad",
  suggestion: "#ffad5a",
  label: "#f2f5f8",
  labelBackground: "#090c12",
};
const TURN_COLORS = ["#ff8a5b", "#56cfe1", "#c77dff", "#f9c74f", "#90be6d", "#f28482", "#4ea8de", "#f8961e"];

const PAN_SURFACE_SIZE = 100000;

function panSurface() {
  return new Konva.Rect({
    x: -PAN_SURFACE_SIZE, y: -PAN_SURFACE_SIZE,
    width: PAN_SURFACE_SIZE * 2, height: PAN_SURFACE_SIZE * 2,
    fill: "rgba(0,0,0,0.001)", listening: true,
  });
}

function placeAnchor(viewport, local, screen) {
  const radians = viewport.rotation() * Math.PI / 180;
  const scale = viewport.scaleX();
  const x = (local.x * Math.cos(radians) - local.y * Math.sin(radians)) * scale;
  const y = (local.x * Math.sin(radians) + local.y * Math.cos(radians)) * scale;
  viewport.position({x: screen.x - x, y: screen.y - y});
}

function editableTarget(target) {
  return target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement;
}

class CircuitProfilerCanvas {
  constructor(container, callbacks = {}) {
    this.container = container;
    this.callbacks = callbacks;
    this.trace = [];
    this.projected = [];
    this.turns = [];
    this.suggestions = [];
    this.armed = null;
    this.selectedTurnIndex = null;
    this.displayMode = "minimal";
    this.traceReference = null;
    this.suggestionReference = null;

    this.stage = new Konva.Stage({container, width: Math.max(320, container.clientWidth), height: 460});
    this.layer = new Konva.Layer();
    this.viewport = new Konva.Group({draggable: true});
    this.panSurface = panSurface();
    this.traceGroup = new Konva.Group();
    this.markerGroup = new Konva.Group();
    this.viewport.add(this.panSurface, this.traceGroup, this.markerGroup);
    this.layer.add(this.viewport);
    this.stage.add(this.layer);

    this.stage.on("wheel", event => this._zoom(event));
    this.container.tabIndex = 0;
    this.container.addEventListener("pointerdown", () => this.container.focus({preventScroll: true}));
    this.keyHandler = event => this._handleKey(event);
    this.container.addEventListener("keydown", this.keyHandler);
    this.viewport.on("dragstart", () => { this.container.style.cursor = "grabbing"; });
    this.viewport.on("dragend", () => { this.container.style.cursor = this.armed ? "crosshair" : "grab"; });
    this.resizeObserver = new ResizeObserver(() => this._resize());
    this.resizeObserver.observe(container);
  }

  destroy() {
    this.resizeObserver.disconnect();
    this.container.removeEventListener("keydown", this.keyHandler);
    this.stage.destroy();
  }

  setTrace(points, suggestions = []) {
    const changed = points !== this.traceReference || suggestions !== this.suggestionReference;
    this.traceReference = points;
    this.suggestionReference = suggestions;
    this.trace = Array.isArray(points) ? points : [];
    this.suggestions = Array.isArray(suggestions) ? suggestions : [];
    if (!changed) return;
    this.viewport.position({x: 0, y: 0});
    this.viewport.scale({x: 1, y: 1});
    this.viewport.rotation(0);
    this._draw();
  }

  setTurns(turns) {
    this.turns = Array.isArray(turns) ? turns : [];
    if (!this.turns.some(turn => turn.rowIndex === this.selectedTurnIndex)) this.selectedTurnIndex = null;
    this._drawMarkers();
  }

  setSelectedTurn(rowIndex) {
    const selected = Number.isInteger(rowIndex) ? rowIndex : null;
    if (selected === this.selectedTurnIndex) return;
    this.selectedTurnIndex = selected;
    this._drawMarkers();
  }

  setDisplayMode(mode) {
    const next = ["minimal", "dots", "ranges"].includes(mode) ? mode : "minimal";
    if (next === this.displayMode) return;
    this.displayMode = next;
    this._drawMarkers();
  }

  setArmed(turn) {
    this.armed = turn || null;
    this.viewport.draggable(true);
    this.container.classList.toggle("pointing", Boolean(this.armed));
  }

  resetView() {
    this.viewport.position({x: 0, y: 0});
    this.viewport.scale({x: 1, y: 1});
    this.viewport.rotation(0);
    this._drawMarkers();
  }

  panBy(x, y) {
    this.viewport.position({x: this.viewport.x() + x, y: this.viewport.y() + y});
    this.layer.batchDraw();
  }

  zoomBy(factor) {
    this._zoomAt({x: this.stage.width() / 2, y: this.stage.height() / 2}, factor);
  }

  rotateBy(degrees) {
    const centre = {x: this.stage.width() / 2, y: this.stage.height() / 2};
    const local = this.viewport.getAbsoluteTransform().copy().invert().point(centre);
    this.viewport.rotation(this.viewport.rotation() + degrees);
    placeAnchor(this.viewport, local, centre);
    this._drawMarkers();
  }

  _resize() {
    const width = Math.max(320, this.container.clientWidth);
    if (width === this.stage.width()) return;
    this.stage.width(width);
    this._draw();
  }

  _draw() {
    this.traceGroup.destroyChildren();
    this.projected = this._project(this.trace);
    if (this.projected.length < 2) {
      this._drawMarkers();
      this.layer.batchDraw();
      return;
    }

    const coordinates = this.projected.flatMap(point => [point.x, point.y]);
    this.traceGroup.add(new Konva.Line({
      points: coordinates, stroke: COLORS.trace, strokeWidth: 3,
      lineCap: "round", lineJoin: "round", listening: false,
      strokeScaleEnabled: false,
    }));
    const hitLine = new Konva.Line({
      points: coordinates, stroke: "rgba(0,0,0,0.001)", strokeWidth: 24,
      lineCap: "round", lineJoin: "round", strokeScaleEnabled: false,
    });
    hitLine.on("pointerclick", event => {
      event.cancelBubble = true;
      if (!this.armed) {
        this.setSelectedTurn(null);
        this.callbacks.onTurnSelected?.(null);
        return;
      }
      const position = this.viewport.getRelativePointerPosition();
      const nearest = this._nearestPosition(position);
      if (nearest) this.callbacks.onApexSelected?.(nearest.distance_m);
    });
    this.traceGroup.add(hitLine);

    const start = this.projected[0];
    this.traceGroup.add(new Konva.Circle({x: start.x, y: start.y, radius: 5, fill: COLORS.trace, listening: false}));
    for (const suggestion of this.suggestions) {
      const point = this._atDistance(Number(suggestion.distance_m));
      if (point) this.traceGroup.add(new Konva.Circle({x: point.x, y: point.y, radius: 3, fill: COLORS.suggestion, listening: false}));
    }
    this._drawMarkers();
    this.layer.batchDraw();
  }

  _drawMarkers() {
    this.markerGroup.destroyChildren();
    if (!this.projected.length) return;
    const inverseScale = 1 / this.viewport.scaleX();
    for (const turn of this.turns) {
      const entry = this._atDistance(turn.entry_m);
      const apex = this._atDistance(turn.apex_m);
      const exit = this._atDistance(turn.exit_m);
      if (!entry || !apex || !exit) continue;
      const selected = turn.rowIndex === this.selectedTurnIndex;
      const turnColor = TURN_COLORS[Math.abs(turn.rowIndex) % TURN_COLORS.length];
      if (selected || this.displayMode === "ranges") {
        const segment = this.projected.filter(point => point.distance_m >= entry.distance_m && point.distance_m <= exit.distance_m);
        const highlighted = [entry, ...segment.filter(point => point !== entry && point !== exit), exit];
        this.markerGroup.add(new Konva.Line({
          points: highlighted.flatMap(point => [point.x, point.y]),
          stroke: turnColor, strokeWidth: selected ? 3.5 : 2.5, opacity: selected ? 1 : 0.68,
          lineCap: "round", lineJoin: "round", listening: false,
          strokeScaleEnabled: false,
        }));
      }
      if (selected) {
        this.markerGroup.add(
          new Konva.Circle({x: entry.x, y: entry.y, radius: 3 * inverseScale, fill: COLORS.labelBackground, stroke: turnColor, strokeWidth: 1.5, strokeScaleEnabled: false, listening: false}),
          new Konva.Circle({x: exit.x, y: exit.y, radius: 3 * inverseScale, fill: COLORS.labelBackground, stroke: turnColor, strokeWidth: 1.5, strokeScaleEnabled: false, listening: false}),
        );
      }
      const compact = !selected;
      const apexNode = new Konva.Circle({
        x: apex.x, y: apex.y, radius: (selected ? 5 : this.displayMode === "minimal" ? 3 : 4) * inverseScale,
        fill: selected ? turnColor : COLORS.labelBackground,
        stroke: selected || this.displayMode !== "minimal" ? turnColor : COLORS.trace,
        strokeWidth: selected ? 2 : 1.5,
        strokeScaleEnabled: false, hitStrokeWidth: 16, draggable: true,
      });
      const margin = ((turn.apex_m - turn.entry_m) + (turn.exit_m - turn.apex_m)) / 2;
      const labelText = selected ? `T${turn.number} · ${apex.distance_m.toFixed(1)} m · ±${margin.toFixed(1)} m` : `T${turn.number}`;
      const label = new Konva.Label({x: apex.x + 11 * inverseScale, y: apex.y - 23 * inverseScale, scaleX: inverseScale, scaleY: inverseScale, listening: false});
      label.add(new Konva.Tag({fill: COLORS.labelBackground, opacity: 0.88, cornerRadius: 4}));
      label.add(new Konva.Text({text: labelText, fill: selected ? COLORS.label : turnColor, fontSize: 12, padding: 5}));
      apexNode.on("pointerclick", event => {
        event.cancelBubble = true;
        this.setSelectedTurn(turn.rowIndex);
        this.callbacks.onTurnSelected?.(turn.rowIndex);
      });
      apexNode.on("pointerenter", () => { this.container.style.cursor = "grab"; });
      apexNode.on("pointerleave", () => { this.container.style.cursor = this.armed ? "crosshair" : ""; });
      apexNode.on("pointerdown", event => { event.cancelBubble = true; });
      apexNode.on("dragstart", event => {
        event.cancelBubble = true;
        this.viewport.draggable(false);
        this.container.style.cursor = "grabbing";
      });
      apexNode.on("dragmove", event => {
        event.cancelBubble = true;
        const nearest = this._nearestPosition(apexNode.position());
        if (!nearest) return;
        apexNode.position({x: nearest.x, y: nearest.y});
        label.position({x: nearest.x + 11 * inverseScale, y: nearest.y - 23 * inverseScale});
        label.getText().text(`T${turn.number} · ${nearest.distance_m.toFixed(1)} m · ±${margin.toFixed(1)} m`);
      });
      apexNode.on("dragend", event => {
        event.cancelBubble = true;
        this.viewport.draggable(true);
        this.container.style.cursor = "grab";
        const nearest = this._nearestPosition(apexNode.position());
        if (!nearest) return;
        const before = Math.max(0, turn.apex_m - turn.entry_m);
        const after = Math.max(0, turn.exit_m - turn.apex_m);
        const maximum = this.trace.reduce((value, point) => Math.max(value, Number(point.distance_m) || 0), nearest.distance_m);
        this.callbacks.onApexMoved?.(turn.rowIndex, {
          apex_m: nearest.distance_m,
          entry_m: Math.max(0, nearest.distance_m - before),
          exit_m: Math.min(maximum, nearest.distance_m + after),
        });
      });
      this.markerGroup.add(apexNode);
      if (!compact || this.displayMode !== "minimal") this.markerGroup.add(label);
    }
    this.layer.batchDraw();
  }

  _project(points) {
    if (!points.length) return [];
    const xs = points.map(point => Number(point.x));
    const zs = points.map(point => Number(point.z));
    const minX = Math.min(...xs), maxX = Math.max(...xs), minZ = Math.min(...zs), maxZ = Math.max(...zs);
    const padding = 36, width = this.stage.width(), height = this.stage.height();
    const scale = Math.min((width - padding * 2) / Math.max(1, maxX - minX), (height - padding * 2) / Math.max(1, maxZ - minZ));
    const usedWidth = (maxX - minX) * scale, usedHeight = (maxZ - minZ) * scale;
    const offsetX = (width - usedWidth) / 2, offsetY = (height - usedHeight) / 2;
    return points.map(point => ({
      ...point,
      distance_m: Number(point.distance_m),
      // F1's game-world X axis is mirrored relative to the conventional
      // broadcast/circuit-map orientation (for example Spa's La Source).
      // Flip only the presentation; stored coordinates remain untouched.
      x: offsetX + usedWidth - (Number(point.x) - minX) * scale,
      y: offsetY + usedHeight - (Number(point.z) - minZ) * scale,
    }));
  }

  _atDistance(distance) {
    if (!Number.isFinite(Number(distance)) || !this.projected.length) return null;
    return this.projected.reduce((best, point) => Math.abs(point.distance_m - distance) < Math.abs(best.distance_m - distance) ? point : best, this.projected[0]);
  }

  _nearestPosition(position) {
    if (!position || !this.projected.length) return null;
    return this.projected.reduce((best, point) => Math.hypot(point.x - position.x, point.y - position.y) < Math.hypot(best.x - position.x, best.y - position.y) ? point : best, this.projected[0]);
  }

  _zoom(event) {
    event.evt.preventDefault();
    if (event.evt.shiftKey) {
      this.rotateBy(event.evt.deltaY > 0 ? 5 : -5);
      return;
    }
    const pointer = this.stage.getPointerPosition();
    if (!pointer) return;
    const direction = event.evt.deltaY > 0 ? -1 : 1;
    this._zoomAt(pointer, direction > 0 ? 1.12 : 1 / 1.12);
  }

  _zoomAt(pointer, factor) {
    const anchor = this.viewport.getAbsoluteTransform().copy().invert().point(pointer);
    const nextScale = Math.max(0.5, Math.min(8, this.viewport.scaleX() * factor));
    this.viewport.scale({x: nextScale, y: nextScale});
    placeAnchor(this.viewport, anchor, pointer);
    this._drawMarkers();
  }

  _handleKey(event) {
    if (editableTarget(event.target)) return;
    const actions = {
      ArrowLeft: () => this.panBy(32, 0), ArrowRight: () => this.panBy(-32, 0),
      ArrowUp: () => this.panBy(0, 32), ArrowDown: () => this.panBy(0, -32),
      "+": () => this.zoomBy(1.15), "=": () => this.zoomBy(1.15), "-": () => this.zoomBy(1 / 1.15),
      q: () => this.rotateBy(-10), Q: () => this.rotateBy(-10), e: () => this.rotateBy(10), E: () => this.rotateBy(10),
      "0": () => this.resetView(),
    };
    const action = actions[event.key];
    if (!action) return;
    event.preventDefault();
    action();
  }
}

const PACE_COLORS = {
  candidate: "#27d9ad",
  baseline: "#f04759",
  neutral: "#8290a3",
  bed: "#293445",
  label: "#f5f7fa",
  labelBackground: "#080b10",
};

class PaceMapCanvas {
  constructor(container, callbacks = {}) {
    this.container = container;
    this.callbacks = callbacks;
    this.points = [];
    this.sections = [];
    this.turns = [];
    this.projected = [];
    this.projection = null;
    this.stage = new Konva.Stage({
      container,
      width: Math.max(320, container.clientWidth),
      height: Math.max(380, container.clientHeight || 500),
    });
    this.layer = new Konva.Layer();
    this.viewport = new Konva.Group({draggable: true});
    this.panSurface = panSurface();
    this.trackGroup = new Konva.Group();
    this.markerGroup = new Konva.Group();
    this.viewport.add(this.panSurface, this.trackGroup, this.markerGroup);
    this.layer.add(this.viewport);
    this.stage.add(this.layer);
    this.container.style.cursor = "grab";
    this.stage.on("wheel", event => this._zoom(event));
    this.container.tabIndex = 0;
    this.container.addEventListener("pointerdown", () => this.container.focus({preventScroll: true}));
    this.keyHandler = event => this._handleKey(event);
    this.container.addEventListener("keydown", this.keyHandler);
    this.viewport.on("dragstart", () => { this.container.style.cursor = "grabbing"; });
    this.viewport.on("dragend", () => { this.container.style.cursor = "grab"; });
    this.resizeObserver = new ResizeObserver(() => this._resize());
    this.resizeObserver.observe(container);
  }

  destroy() {
    this.resizeObserver.disconnect();
    this.container.removeEventListener("keydown", this.keyHandler);
    this.stage.destroy();
  }

  setData(points, sections, turns) {
    this.points = Array.isArray(points) ? points : [];
    this.sections = Array.isArray(sections) ? sections : [];
    this.turns = Array.isArray(turns) ? turns : [];
    this.resetView();
    this._draw();
  }

  resetView() {
    this.viewport.position({x: 0, y: 0});
    this.viewport.scale({x: 1, y: 1});
    this.viewport.rotation(0);
    this._drawMarkers();
  }

  panBy(x, y) {
    this.viewport.position({x: this.viewport.x() + x, y: this.viewport.y() + y});
    this.layer.batchDraw();
  }

  zoomBy(factor) {
    this._zoomAt({x: this.stage.width() / 2, y: this.stage.height() / 2}, factor);
  }

  rotateBy(degrees) {
    const centre = {x: this.stage.width() / 2, y: this.stage.height() / 2};
    const local = this.viewport.getAbsoluteTransform().copy().invert().point(centre);
    this.viewport.rotation(this.viewport.rotation() + degrees);
    placeAnchor(this.viewport, local, centre);
    this._drawMarkers();
  }

  _resize() {
    const width = Math.max(320, this.container.clientWidth);
    const height = Math.max(380, this.container.clientHeight || 500);
    if (width === this.stage.width() && height === this.stage.height()) return;
    this.stage.size({width, height});
    this._draw();
  }

  _draw() {
    this.trackGroup.destroyChildren();
    this.projection = this._projectionFor(this.points);
    this.projected = this.points.map(point => ({...point, ...this._project(point)}));
    if (this.projected.length < 2) {
      this._drawMarkers();
      this.layer.batchDraw();
      return;
    }
    const fullTrack = this.projected.flatMap(point => [point.x, point.y]);
    this.trackGroup.add(new Konva.Line({
      points: fullTrack, stroke: PACE_COLORS.bed, strokeWidth: 13,
      lineCap: "round", lineJoin: "round", listening: false,
      strokeScaleEnabled: false,
    }));
    for (const section of this.sections) {
      const projected = section.points.map(point => this._project(point));
      const line = new Konva.Line({
        points: projected.flatMap(point => [point.x, point.y]),
        stroke: PACE_COLORS[section.kind] || PACE_COLORS.neutral,
        strokeWidth: 7, lineCap: "round", lineJoin: "round",
        hitStrokeWidth: 18, strokeScaleEnabled: false,
      });
      line.on("pointerenter pointermove", event => {
        this.container.style.cursor = "pointer";
        this.callbacks.onHover?.({type: "section", ...section}, event.evt);
      });
      line.on("pointerleave", () => {
        this.container.style.cursor = "grab";
        this.callbacks.onLeave?.();
      });
      this.trackGroup.add(line);
    }
    this._drawMarkers();
    this.layer.batchDraw();
  }

  _drawMarkers() {
    this.markerGroup.destroyChildren();
    if (!this.projected.length) return;
    const inverseScale = 1 / this.viewport.scaleX();
    for (const turn of this.turns) {
      const point = this._atDistance(turn.apex_m);
      if (!point) continue;
      const color = PACE_COLORS[turn.kind] || PACE_COLORS.neutral;
      const marker = new Konva.Circle({
        x: point.x, y: point.y, radius: 6 * inverseScale,
        fill: color, stroke: PACE_COLORS.label, strokeWidth: 1.5,
        strokeScaleEnabled: false, hitStrokeWidth: 16,
      });
      const label = new Konva.Label({
        x: point.x + 10 * inverseScale,
        y: point.y + (turn.labelAbove ? -25 : 9) * inverseScale,
        scaleX: inverseScale, scaleY: inverseScale, listening: false,
      });
      label.add(new Konva.Tag({fill: PACE_COLORS.labelBackground, opacity: 0.82, cornerRadius: 4}));
      label.add(new Konva.Text({text: `T${turn.number}`, fill: PACE_COLORS.label, fontSize: 12, fontStyle: "bold", padding: 4}));
      marker.on("pointerenter pointermove", event => {
        this.container.style.cursor = "pointer";
        this.callbacks.onHover?.({type: "turn", ...turn}, event.evt);
      });
      marker.on("pointerleave", () => {
        this.container.style.cursor = "grab";
        this.callbacks.onLeave?.();
      });
      this.markerGroup.add(marker, label);
    }
    this.layer.batchDraw();
  }

  _projectionFor(points) {
    if (!points.length) return null;
    const xs = points.map(point => Number(point.x));
    const zs = points.map(point => Number(point.z));
    const minX = Math.min(...xs), maxX = Math.max(...xs), minZ = Math.min(...zs), maxZ = Math.max(...zs);
    const padding = 48, width = this.stage.width(), height = this.stage.height();
    const scale = Math.min((width - padding * 2) / Math.max(1, maxX - minX), (height - padding * 2) / Math.max(1, maxZ - minZ));
    const usedWidth = (maxX - minX) * scale, usedHeight = (maxZ - minZ) * scale;
    return {minX, minZ, scale, offsetX: (width - usedWidth) / 2, offsetY: (height - usedHeight) / 2, usedWidth, usedHeight};
  }

  _project(point) {
    const p = this.projection;
    if (!p) return {x: 0, y: 0};
    return {
      // Match the conventional circuit-map orientation used by the profiler.
      x: p.offsetX + p.usedWidth - (Number(point.x) - p.minX) * p.scale,
      y: p.offsetY + p.usedHeight - (Number(point.z) - p.minZ) * p.scale,
    };
  }

  _atDistance(distance) {
    if (!Number.isFinite(Number(distance)) || !this.projected.length) return null;
    return this.projected.reduce((best, point) => Math.abs(Number(point.distance_m) - distance) < Math.abs(Number(best.distance_m) - distance) ? point : best, this.projected[0]);
  }

  _zoom(event) {
    event.evt.preventDefault();
    if (event.evt.shiftKey) {
      this.rotateBy(event.evt.deltaY > 0 ? 5 : -5);
      return;
    }
    const pointer = this.stage.getPointerPosition();
    if (!pointer) return;
    const direction = event.evt.deltaY > 0 ? -1 : 1;
    this._zoomAt(pointer, direction > 0 ? 1.12 : 1 / 1.12);
  }

  _zoomAt(pointer, factor) {
    const anchor = this.viewport.getAbsoluteTransform().copy().invert().point(pointer);
    const nextScale = Math.max(0.5, Math.min(8, this.viewport.scaleX() * factor));
    this.viewport.scale({x: nextScale, y: nextScale});
    placeAnchor(this.viewport, anchor, pointer);
    this._drawMarkers();
  }

  _handleKey(event) {
    if (editableTarget(event.target)) return;
    const actions = {
      ArrowLeft: () => this.panBy(32, 0), ArrowRight: () => this.panBy(-32, 0),
      ArrowUp: () => this.panBy(0, 32), ArrowDown: () => this.panBy(0, -32),
      "+": () => this.zoomBy(1.15), "=": () => this.zoomBy(1.15), "-": () => this.zoomBy(1 / 1.15),
      q: () => this.rotateBy(-10), Q: () => this.rotateBy(-10), e: () => this.rotateBy(10), E: () => this.rotateBy(10),
      "0": () => this.resetView(),
    };
    const action = actions[event.key];
    if (!action) return;
    event.preventDefault();
    action();
  }
}

window.CircuitProfilerCanvas = CircuitProfilerCanvas;
window.PaceMapCanvas = PaceMapCanvas;
