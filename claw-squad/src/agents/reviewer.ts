/**
 * Reviewer agent: examine a diff, produce structured verdict.
 *
 * Token efficiency: the Reviewer only sees the diff + the TODO, NOT the full
 * file contents. A 500-line file with a 10-line change sends 10 lines to the
 * Reviewer, not 500. This is the single biggest reason we split Coder and
 * Reviewer into separate agents: their context shapes are different.
 *
 * Provider-agnostic: takes a Provider instance so it can target any backend.
 */

import { loadPrompt } from "../prompts.js";
import type { InvokeResult, Provider } from "../providers/types.js";
import { appendEvent, type RunLogHandle } from "../runs/log.js";
import { emitRunIoIfEnabled } from "../runs/dataset.js";
import type { ReviewVerdict, TodoItem } from "../types.js";

export interface ReviewerOutcome {
  verdict: ReviewVerdict;
  message: string;
  usage: InvokeResult;
}

interface ReviewerInput {
  todo: TodoItem;
  diff: string;
  coderRationale?: string;
  provider: Provider;
  onText?: (chunk: string) => void;
  // Optional: when present + CLAW_SQUAD_LOG_PROMPTS is set, the run
  // log captures (prompt, response) so `claw-squad dataset export`
  // has training data to mine. Off by default; the orchestrator
  // threads the handle through.
  runLog?: RunLogHandle;
  // Optional sibling-file context. Fed by the orchestrator when
  // `--smart-context` is on: top-K embedding-index hits for the
  // todo description, MINUS any file already covered by the diff
  // (those land in the diff anyway). Helps the Reviewer catch
  // "did the Coder break the caller of this function" without
  // bloating prompts when there's nothing relevant.
  siblingContext?: Array<{ path: string; content: string }>;
}

function buildUserMessage(input: ReviewerInput): string {
  const parts: string[] = [];
  parts.push(`## TODO (${input.todo.id})`);
  parts.push(`Title: ${input.todo.title}`);
  parts.push(`Description: ${input.todo.description}`);
  parts.push("");
  if (input.coderRationale) {
    parts.push("## Coder rationale");
    parts.push(input.coderRationale.trim());
    parts.push("");
  }
  // Sibling context renders BEFORE the diff so the Reviewer reads
  // "this is the surrounding code" → "this is what changed", not the
  // other way round. The header is explicit so the Reviewer doesn't
  // hallucinate that these files are part of the change.
  if (input.siblingContext && input.siblingContext.length > 0) {
    parts.push(
      "## Sibling files (context only — NOT part of the diff)",
    );
    for (const f of input.siblingContext) {
      parts.push(`### \`${f.path}\``);
      parts.push("```");
      parts.push(f.content);
      parts.push("```");
      parts.push("");
    }
  }
  parts.push("## Diff");
  parts.push("```diff");
  parts.push(input.diff);
  parts.push("```");
  parts.push("");
  parts.push("Produce the review JSON per the system prompt.");
  return parts.join("\n");
}

export function parseReviewerOutput(text: string): ReviewVerdict {
  const jsonMatch = text.match(/```json\s*([\s\S]*?)```/);
  if (!jsonMatch?.[1]) {
    // Defensive: if the Reviewer didn't return JSON, treat as request_changes
    // to force a retry rather than silently approving.
    return {
      decision: "request_changes",
      summary: "Reviewer did not return a parseable verdict; requesting a re-review.",
      findings: [
        {
          severity: "high",
          issue: "No ```json block in the response.",
          suggestion: "Re-run the Reviewer.",
        },
      ],
    };
  }
  try {
    const parsed = JSON.parse(jsonMatch[1]);
    const decision: ReviewVerdict["decision"] =
      parsed.decision === "approve" ? "approve" : "request_changes";
    return {
      decision,
      summary: String(parsed.summary ?? ""),
      findings: Array.isArray(parsed.findings) ? parsed.findings : [],
    };
  } catch (err) {
    return {
      decision: "request_changes",
      summary: `Reviewer JSON parse failed: ${(err as Error).message}`,
      findings: [],
    };
  }
}

export async function runReviewer(
  input: ReviewerInput,
): Promise<ReviewerOutcome> {
  const systemPrompt = loadPrompt("reviewer");
  const userMessage = buildUserMessage(input);

  const usage = await input.provider.invoke({
    role: "reviewer",
    systemPrompt,
    userMessage,
    onText: input.onText,
  });

  // Parse before emitting so the run-io event can carry the verdict
  // (W11.5). Lets `dataset export --review-decision approve` filter
  // a fine-tune corpus to runs that actually shipped without
  // re-parsing every reviewer response downstream.
  const verdict = parseReviewerOutput(usage.text);

  emitRunIoIfEnabled((e) => appendEvent(input.runLog!, e), {
    runLog: input.runLog,
    role: "reviewer",
    prompt: userMessage,
    responseText: usage.text,
    reviewDecision: verdict.decision,
  });

  return {
    verdict,
    message: usage.text,
    usage,
  };
}
