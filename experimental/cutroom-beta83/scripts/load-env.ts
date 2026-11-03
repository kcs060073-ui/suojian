/**
 * 零依赖 .env 加载器：import 即生效。把项目根 .env 的 KEY=VALUE 灌进 process.env。
 * 已存在的环境变量不覆盖（shell 里手动 set 的优先）。在各脚本最顶部 `import "./load-env";`。
 */
import * as fs from "fs";
import * as path from "path";

const envPath = path.resolve(__dirname, "..", ".env");
if (fs.existsSync(envPath)) {
  for (const line of fs.readFileSync(envPath, "utf8").split(/\r?\n/)) {
    if (!line || /^\s*#/.test(line)) continue;
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2].replace(/^["']|["']$/g, "");
  }
}
