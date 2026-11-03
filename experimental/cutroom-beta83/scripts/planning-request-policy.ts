import OpenAI from "openai";
import { parseJsonObjectLoose, type StructuredOutputParser } from "./model-json-output";

interface PlanningRequest {
  apiKey: string;
  baseURL: string;
  model: string;
  messages: OpenAI.Chat.Completions.ChatCompletionMessageParam[];
}

export class PlanningOutputError extends Error {
  constructor(
    readonly code: "MODEL_OUTPUT_EMPTY" | "MODEL_OUTPUT_TRUNCATED" | "MODEL_OUTPUT_INVALID",
    readonly diagnostics: {
      finishReason: string | null;
      contentLength: number;
      hasReasoning: boolean;
      promptTokens: number | null;
      completionTokens: number | null;
      validationStage?: "json" | "schema";
      validationCode?: string;
    },
  ) {
    super(`${code}: ${JSON.stringify(diagnostics)}`);
    this.name = "PlanningOutputError";
  }
}

export async function requestStructuredPlanning<T>(
  request: PlanningRequest,
  parse: StructuredOutputParser<T>,
  timeoutMs = 120_000,
): Promise<T> {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 120_000) {
    throw new Error("Planning timeout must be between 0 and 120000 ms");
  }
  // Each planning stage permits one paid attempt, including malformed responses.
  const client = new OpenAI({ apiKey: request.apiKey, baseURL: request.baseURL,
    maxRetries: 0, timeout: timeoutMs });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await client.chat.completions.create({
      model: request.model,
      temperature: 0.3,
      max_tokens: 4096,
      // This model defaults to maximum thinking, which can exhaust the answer budget.
      ...(request.model.toLowerCase() === "glm-5.3-flash"
        ? { reasoning_effort: "low" as const } : {}),
      messages: request.messages,
    }, { signal: controller.signal });
    const choice = response.choices?.[0];
    const content = typeof choice?.message?.content === "string" ? choice.message.content : "";
    const reasoning = (choice?.message as { reasoning_content?: unknown } | undefined)?.reasoning_content;
    const tokenCount = (value: unknown): number | null =>
      typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
    const diagnostics = {
      finishReason: ["stop", "length", "tool_calls", "content_filter", "function_call"]
        .includes(choice?.finish_reason || "") ? choice.finish_reason : null,
      contentLength: content.length,
      hasReasoning: typeof reasoning === "string" && reasoning.length > 0,
      promptTokens: tokenCount(response.usage?.prompt_tokens),
      completionTokens: tokenCount(response.usage?.completion_tokens),
    };
    if (diagnostics.finishReason === "length") {
      throw new PlanningOutputError("MODEL_OUTPUT_TRUNCATED", diagnostics);
    }
    if (!content.trim()) throw new PlanningOutputError("MODEL_OUTPUT_EMPTY", diagnostics);
    let value: unknown;
    try {
      value = parseJsonObjectLoose(content);
    } catch {
      throw new PlanningOutputError("MODEL_OUTPUT_INVALID", {
        ...diagnostics, validationStage: "json", validationCode: "JSON_SYNTAX_INVALID",
      });
    }
    try {
      return parse(value);
    } catch (error) {
      // Only known application codes may leave the parser. Zod messages, model
      // text and arbitrary exception strings can contain the user's script.
      const knownCodes = ["SCRIPT_SEGMENT_GROUPS_INVALID", "STORYBOARD_PHRASE_TOO_LONG"];
      const validationCode = error instanceof Error
        ? knownCodes.find(code => error.message.startsWith(`${code}:`)) : undefined;
      throw new PlanningOutputError("MODEL_OUTPUT_INVALID", {
        ...diagnostics, validationStage: "schema", validationCode: validationCode || "SCHEMA_INVALID",
      });
    }
  } finally {
    clearTimeout(timer);
  }
}
