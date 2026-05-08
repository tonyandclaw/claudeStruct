// Public re-exports. The package consumer imports everything
// from here:
//
//   import { Client, AuthError } from "@claudestruct/sdk";

export { Client } from "./client.js";
export type { ClientOptions } from "./client.js";

export {
  ApiError,
  AuthError,
  BudgetExceededError,
  ForbiddenError,
  NotFoundError,
  ServerError,
} from "./errors.js";

export type {
  AuthorRollup,
  CreateRunRequest,
  CreateRunResponse,
  DashboardResponse,
  KeyMetadata,
  RunRow,
  TaskRollup,
  TeamDashboardResponse,
} from "./types.js";

export const SDK_VERSION = "0.0.1-alpha";
