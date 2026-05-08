// Typed exception hierarchy. Application code can `catch (e)`
// then narrow with `instanceof AuthError` etc.

export class ApiError extends Error {
  readonly statusCode: number;
  readonly detail?: string;

  constructor(statusCode: number, detail?: string) {
    super(`${statusCode}: ${detail ?? "<no detail>"}`);
    this.name = "ApiError";
    this.statusCode = statusCode;
    this.detail = detail;
  }
}

export class AuthError extends ApiError {
  constructor(detail?: string) {
    super(401, detail);
    this.name = "AuthError";
  }
}

export class ForbiddenError extends ApiError {
  constructor(detail?: string) {
    super(403, detail);
    this.name = "ForbiddenError";
  }
}

export class NotFoundError extends ApiError {
  constructor(detail?: string) {
    super(404, detail);
    this.name = "NotFoundError";
  }
}

export class BudgetExceededError extends ApiError {
  constructor(detail?: string) {
    super(402, detail);
    this.name = "BudgetExceededError";
  }
}

export class ServerError extends ApiError {
  constructor(statusCode: number, detail?: string) {
    super(statusCode, detail);
    this.name = "ServerError";
  }
}
