export class RequestGeneration {
  constructor() {
    this.value = 0;
  }

  issue() {
    this.value += 1;
    return this.value;
  }

  invalidate() {
    this.value += 1;
  }

  isCurrent(generation) {
    return generation === this.value;
  }
}

export function createDashboardState() {
  return {
    sessionsById: new Map(),
    lapsById: new Map(),
    lapsBySession: new Map(),
    timelineBySession: new Map(),
    timelineRequests: new Map(),
    libraryGeneration: new RequestGeneration(),
    comparisonGeneration: new RequestGeneration(),
  };
}
