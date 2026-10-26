/** Errors thrown by CerebelClient. Each subclass maps to one way the API says no. */

export type Payload = Record<string, unknown>;

export class CerebelError extends Error {
  readonly status: number;
  readonly payload: Payload;

  constructor(message: string, status = 0, payload: Payload = {}) {
    super(message);
    this.name = new.target.name;
    this.status = status;
    this.payload = payload;
  }

  /** Server-side reference for a 500 answer; quote it when reporting a bug. */
  get errorId(): string | undefined {
    const id = this.payload["error_id"];
    return typeof id === "string" ? id : undefined;
  }
}

/** 401: not signed in, token expired (30 days by default) or wrong password. */
export class AuthError extends CerebelError {}

/** 402: the monthly analysis allowance is used up. */
export class PaymentRequired extends CerebelError {
  get quota(): Payload {
    return (this.payload["quota"] as Payload) ?? {};
  }
}

/** 403: the capture belongs to another account, or the action is operator-only. */
export class Forbidden extends CerebelError {}

/** 404: unknown capture or endpoint, or nothing to show yet. */
export class NotFound extends CerebelError {}

/** 410: the account deleted this capture. */
export class Gone extends CerebelError {}

/** 422: the video failed the quality check. It did not use an analysis. */
export class UploadRejected extends CerebelError {}

/** 428: agree to the current video-processing text first (see `consent.text`). */
export class ConsentRequired extends CerebelError {
  get consent(): Payload {
    return (this.payload["video_consent"] as Payload) ?? {};
  }
}

/** 429: today's coach-question allowance is used up (resets at midnight UTC). */
export class RateLimited extends CerebelError {}

/** 503: the GPU that rebuilds bodies is offline. Nothing was uploaded; retry later. */
export class ServiceUnavailable extends CerebelError {}

/** The upload was accepted but its analysis ended in `error`, `no_swing` or `rejected`. */
export class AnalysisFailed extends CerebelError {
  get stage(): string {
    return String(this.payload["stage"] ?? "");
  }
}

const BY_STATUS: Record<number, typeof CerebelError> = {
  401: AuthError,
  402: PaymentRequired,
  403: Forbidden,
  404: NotFound,
  410: Gone,
  422: UploadRejected,
  428: ConsentRequired,
  429: RateLimited,
  503: ServiceUnavailable,
};

export function errorFor(status: number, payload: unknown): CerebelError {
  const body = payload && typeof payload === "object" ? (payload as Payload) : {};
  const message = typeof body["message"] === "string" ? (body["message"] as string) : `HTTP ${status}`;
  const Cls = BY_STATUS[status] ?? CerebelError;
  return new Cls(message, status, body);
}
