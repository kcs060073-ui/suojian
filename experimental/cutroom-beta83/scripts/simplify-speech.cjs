// Text-only traditional-to-simplified mapping for evidence-preserving alignment.
const OpenCC = require("opencc-js");
const convert = OpenCC.Converter({ from: "tw", to: "cn" });
let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", chunk => { input += chunk; if (input.length > 200000) process.exit(2); });
process.stdin.on("end", () => {
  const chars = JSON.parse(input);
  if (!Array.isArray(chars) || chars.some(c => typeof c !== "string" || [...c].length !== 1)) process.exit(2);
  process.stdout.write(JSON.stringify(Object.fromEntries(chars.map(c => [c, convert(c)]))));
});
