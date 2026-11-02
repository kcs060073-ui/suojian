export type StructuredOutputParser<T> = (value: unknown) => T;
export type StructuredOutputRepair = (invalidOutput: string) => Promise<string>;

export const JSON_REPAIR_SYSTEM_PROMPT = `你是严格的 JSON 语法修复器。
只修复所给模型输出的 JSON 语法，使其成为一个可被 JSON.parse 解析的 JSON 对象。
保留原有字段、数组顺序和业务文字，不新增、删减、改写业务内容；仅处理未转义引号、尾随逗号等 JSON 语法问题。
只输出修复后的 JSON 对象，不要解释，不要 markdown 代码块。`;

export function parseJsonObjectLoose(output: string): unknown {
  let text = output.trim();
  const fence = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fence) text = fence[1].trim();
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start >= 0 && end > start) text = text.slice(start, end + 1);
  return JSON.parse(text);
}

export async function parseStructuredOutputWithSingleRepair<T>(
  output: string,
  parse: StructuredOutputParser<T>,
  repair: StructuredOutputRepair,
): Promise<T> {
  try {
    return parse(parseJsonObjectLoose(output));
  } catch {
    const repaired = await repair(output);
    try {
      return parse(parseJsonObjectLoose(repaired));
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      throw new Error(`修复后仍不是有效的预期 JSON 结构: ${detail}`);
    }
  }
}
