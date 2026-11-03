// Capture a real Chromium viewport at the requested CSS pixel size.
// Usage: node scripts/capture_browser_page.mjs URL OUTPUT WIDTH HEIGHT [WAIT_SELECTOR]
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve, sep } from "node:path";

const [url, output, widthArg, heightArg, waitSelector = "body"] = process.argv.slice(2);
const width = Number(widthArg);
const height = Number(heightArg);
if (!url || !output || !Number.isInteger(width) || !Number.isInteger(height) || width < 320 || height < 320) {
  throw new Error("Usage: URL OUTPUT WIDTH HEIGHT [WAIT_SELECTOR]");
}
const chrome = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
].find(existsSync);
if (!chrome) throw new Error("Chrome or Edge not found");
const profileRoot = resolve(dirname(output));
const profile = mkdtempSync(join(profileRoot, ".chrome-capture-"));
const child = spawn(chrome, [
  "--headless=new", "--disable-gpu", "--no-first-run", "--remote-debugging-port=0",
  "--remote-allow-origins=*", "--allow-file-access-from-files",
  ...(process.env.CUTROOM_CAPTURE_DARK === "1" ? ["--force-dark-mode"] : []),
  `--user-data-dir=${profile}`, "--window-size=1024,900", url,
], { stdio: "ignore" });
let socket;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
try {
  const activePort = join(profile, "DevToolsActivePort");
  for (let i = 0; i < 100 && !existsSync(activePort); i++) await sleep(100);
  if (!existsSync(activePort)) throw new Error("Chrome debugging port did not start");
  let port;
  for (let i = 0; i < 30; i++) {
    try {
      port = Number(readFileSync(activePort, "utf8").split(/\r?\n/)[0]);
      if (Number.isInteger(port) && port > 0) break;
    } catch (error) {
      if (error.code !== "EBUSY" && error.code !== "EACCES") throw error;
    }
    await sleep(100);
  }
  if (!Number.isInteger(port) || port <= 0) throw new Error("Chrome debugging port unavailable");
  let pages = [];
  for (let i = 0; i < 50; i++) {
    pages = await fetch(`http://127.0.0.1:${port}/json/list`).then((response) => response.json());
    if (pages.some((page) => page.type === "page")) break;
    await sleep(100);
  }
  const expectedOrigin = new URL(url).origin;
  const page = pages.find((item) => item.type === "page" &&
    (expectedOrigin === "null" ? item.url.startsWith("file:") : item.url.startsWith(expectedOrigin)))
    || pages.find((item) => item.type === "page");
  if (!page) throw new Error("No Chrome page target");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  let sequence = 0;
  const pending = new Map();
  socket.addEventListener("message", ({ data }) => {
    const response = JSON.parse(data);
    if (!response.id || !pending.has(response.id)) return;
    const [resolve, reject] = pending.get(response.id);
    pending.delete(response.id);
    response.error ? reject(new Error(response.error.message)) : resolve(response.result);
  });
  const command = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    pending.set(id, [resolve, reject]);
    socket.send(JSON.stringify({ id, method, params }));
  });
  await command("Emulation.setDeviceMetricsOverride", {
    width, height, deviceScaleFactor: 1, mobile: false,
  });
  let state = null;
  for (let i = 0; i < 150; i++) {
    const observed = await command("Runtime.evaluate", {
      expression: `JSON.stringify({ready:document.readyState,width:innerWidth,scrollWidth:document.documentElement.scrollWidth,found:!!document.querySelector(${JSON.stringify(waitSelector)})})`,
      returnByValue: true,
    });
    if (!observed.result?.value) {
      await sleep(100);
      continue;
    }
    state = JSON.parse(observed.result.value);
    if (state.ready === "complete" && state.found) break;
    await sleep(100);
  }
  if (!state?.found || state.width !== width) throw new Error(`Wrong viewport or page: ${JSON.stringify(state)}`);
  const readyVideoSelector = process.env.CUTROOM_CAPTURE_READY_VIDEO_SELECTOR;
  if (readyVideoSelector) {
    let mediaReady = false;
    for (let i = 0; i < 150; i++) {
      const observed = await command("Runtime.evaluate", {
        expression: `(() => { const video = document.querySelector(${JSON.stringify(readyVideoSelector)}); return Boolean(video && video.readyState >= 2 && video.videoWidth > 0); })()`,
        returnByValue: true,
      });
      mediaReady = Boolean(observed.result?.value);
      if (mediaReady) break;
      await sleep(100);
    }
    if (!mediaReady) throw new Error(`Capture video never became ready: ${readyVideoSelector}`);
  }
  const nativeVideoClickSelector = process.env.CUTROOM_CAPTURE_NATIVE_VIDEO_CLICK_SELECTOR;
  if (nativeVideoClickSelector) {
    const observed = await command("Runtime.evaluate", {
      expression: `(() => { const video = document.querySelector(${JSON.stringify(nativeVideoClickSelector)}); if (!video) return null; const rect = video.getBoundingClientRect(); return JSON.stringify({x:rect.left + 40,y:rect.bottom - 64}); })()`,
      returnByValue: true,
    });
    if (!observed.result?.value) throw new Error(`Capture video click target missing: ${nativeVideoClickSelector}`);
    const point = JSON.parse(observed.result.value);
    await command("Input.dispatchMouseEvent", { type: "mousePressed", x: point.x, y: point.y, button: "left", clickCount: 1 });
    await command("Input.dispatchMouseEvent", { type: "mouseReleased", x: point.x, y: point.y, button: "left", clickCount: 1 });
  }
  const clickSelector = process.env.CUTROOM_CAPTURE_CLICK_SELECTOR;
  if (clickSelector) {
    // The document can be complete before the deferred workbench module binds navigation.
    await sleep(700);
    const beforeClickText = process.env.CUTROOM_CAPTURE_BEFORE_CLICK_TEXT;
    if (beforeClickText) {
      let ready = false;
      for (let i = 0; i < 100; i++) {
        const observed = await command("Runtime.evaluate", {
          expression: `document.body.innerText.includes(${JSON.stringify(beforeClickText)})`,
          returnByValue: true,
        });
        ready = Boolean(observed.result?.value);
        if (ready) break;
        await sleep(100);
      }
      if (!ready) throw new Error(`Capture pre-click text missing: ${beforeClickText}`);
    }
    const afterClickSelector = process.env.CUTROOM_CAPTURE_AFTER_CLICK_SELECTOR || clickSelector;
    let clicked;
    for (let i = 0; i < 150; i++) {
      clicked = await command("Runtime.evaluate", {
        expression: `(() => { const target = document.querySelector(${JSON.stringify(clickSelector)}); if (!target) return false; target.click(); return true; })()`,
        returnByValue: true,
      });
      if (clicked.result?.value) break;
      await sleep(100);
    }
    if (!clicked.result?.value) throw new Error(`Capture click target missing: ${clickSelector}`);
    for (let i = 0; i < 100; i++) {
      const ready = await command("Runtime.evaluate", {
        expression: `Boolean(document.querySelector(${JSON.stringify(afterClickSelector)}))`,
        returnByValue: true,
      });
      if (ready.result?.value) break;
      if (i > 0 && i % 15 === 0) {
        await command("Runtime.evaluate", {
          expression: `document.querySelector(${JSON.stringify(clickSelector)})?.click()`,
          returnByValue: true,
        });
      }
      await sleep(100);
    }
    const ready = await command("Runtime.evaluate", {
      expression: `Boolean(document.querySelector(${JSON.stringify(afterClickSelector)}))`,
      returnByValue: true,
    });
    if (!ready.result?.value) throw new Error(`Capture result missing: ${afterClickSelector}`);
    const afterClickSettleMs = Number(process.env.CUTROOM_CAPTURE_AFTER_CLICK_SETTLE_MS || 0);
    if (!Number.isFinite(afterClickSettleMs) || afterClickSettleMs < 0 || afterClickSettleMs > 10_000) {
      throw new Error("Invalid after-click settle time");
    }
    if (afterClickSettleMs) await sleep(afterClickSettleMs);
    const secondClickSelector = process.env.CUTROOM_CAPTURE_SECOND_CLICK_SELECTOR;
    if (secondClickSelector) {
      const second = await command("Runtime.evaluate", {
        expression: `(() => { const target = document.querySelector(${JSON.stringify(secondClickSelector)}); if (!target) return false; target.click(); return true; })()`,
        returnByValue: true,
      });
      if (!second.result?.value) throw new Error(`Capture second click target missing: ${secondClickSelector}`);
    }
    const actions = JSON.parse(process.env.CUTROOM_CAPTURE_ACTIONS || "[]");
    if (!Array.isArray(actions)) throw new Error("Capture actions must be an array");
    for (const action of actions) {
      if (!["click", "input", "select", "waitText", "waitSelector", "reload"].includes(action.type)) {
        throw new Error(`Unsupported capture action: ${action.type}`);
      }
      if (action.type === "reload") {
        await command("Page.reload", { ignoreCache: true });
        await sleep(700);
        continue;
      }
      let done = false;
      for (let i = 0; i < (action.type === "waitText" ? 450 : 150); i++) {
        const expression = action.type === "waitText"
          ? `document.body.innerText.includes(${JSON.stringify(action.text || "")})`
          : action.type === "waitSelector"
            ? `Boolean(document.querySelector(${JSON.stringify(action.selector)}))`
            : action.type === "click"
              ? `(() => { const target = document.querySelector(${JSON.stringify(action.selector)}); if (!target || target.disabled) return false; target.click(); return true; })()`
              : action.type === "select"
                ? `(() => { const target = document.querySelector(${JSON.stringify(action.selector)}); if (!target) return false; target.value = ${JSON.stringify(action.value || "")}; target.dispatchEvent(new Event('change', {bubbles:true})); return true; })()`
              : `(() => { const target = document.querySelector(${JSON.stringify(action.selector)}); if (!target) return false; target.value = ${JSON.stringify(action.value || "")}; target.dispatchEvent(new Event('input', {bubbles:true})); return true; })()`;
        const observed = await command("Runtime.evaluate", { expression, returnByValue: true });
        done = Boolean(observed.result?.value);
        if (done) break;
        await sleep(100);
      }
      if (!done) {
        const current = await command("Runtime.evaluate", {
          expression: "document.body.innerText.slice(-1000)", returnByValue: true,
        });
        throw new Error(`Capture action failed: ${JSON.stringify(action)}; page: ${current.result?.value}`);
      }
      if (process.env.CUTROOM_CAPTURE_TRACE_ACTIONS === "1") {
        const observed = await command("Runtime.evaluate", {
          expression: "JSON.stringify({hash:location.hash,step:document.querySelector('#create-step-status')?.textContent,selectedRoute:document.querySelector('#route-options [aria-pressed=true]')?.dataset.route,title:document.querySelector('[name=title]')?.value,primary:document.querySelector('[name=primary_library_id]')?.value,voice:document.querySelector('[name=tts_voice_id]')?.value,routeHidden:document.querySelector('[data-create-panel=route]')?.hidden,inputHidden:document.querySelector('[data-create-panel=input]')?.hidden,reviewHidden:document.querySelector('#preflight-panel')?.hidden,nextDisabled:document.querySelector('[data-create-next]')?.disabled,detailsOpen:document.querySelector('#create-preflight-details')?.open,errors:document.querySelector('#create-errors')?.innerText})",
          returnByValue: true,
        });
        process.stderr.write(`${action.type}: ${observed.result?.value}\n`);
      }
    }
    const inputSelector = process.env.CUTROOM_CAPTURE_INPUT_SELECTOR;
    if (inputSelector) {
      const inputValue = process.env.CUTROOM_CAPTURE_INPUT_VALUE || "";
      const typed = await command("Runtime.evaluate", {
        expression: `(() => { const target = document.querySelector(${JSON.stringify(inputSelector)}); if (!target) return false; target.value = ${JSON.stringify(inputValue)}; target.dispatchEvent(new Event('input', {bubbles:true})); return true; })()`,
        returnByValue: true,
      });
      if (!typed.result?.value) throw new Error(`Capture input target missing: ${inputSelector}`);
    }
    const waitText = process.env.CUTROOM_CAPTURE_WAIT_TEXT;
    if (waitText) {
      let found = false;
      for (let i = 0; i < 100; i++) {
        const observed = await command("Runtime.evaluate", {
          expression: `document.body.innerText.includes(${JSON.stringify(waitText)})`,
          returnByValue: true,
        });
        found = Boolean(observed.result?.value);
        if (found) break;
        await sleep(100);
      }
      if (!found) throw new Error(`Capture wait text missing: ${waitText}`);
    }
    const playSelector = process.env.CUTROOM_CAPTURE_PLAY_SELECTOR;
    if (playSelector) {
      let started = false;
      for (let i = 0; i < 100; i++) {
        const playback = await command("Runtime.evaluate", {
          expression: `(() => { const media = document.querySelector(${JSON.stringify(playSelector)}); if (!media || media.readyState < 2) return false; media.muted = true; media.play(); return true; })()`,
          returnByValue: true,
        });
        if (playback.result?.value) { started = true; break; }
        await sleep(100);
      }
      if (!started) throw new Error(`Capture playback unavailable: ${playSelector}`);
      await sleep(1200);
    }
    const scrollSelector = process.env.CUTROOM_CAPTURE_SCROLL_SELECTOR;
    if (scrollSelector) {
      let found = false;
      for (let i = 0; i < 100; i++) {
        const scrolled = await command("Runtime.evaluate", {
          expression: `(() => { const target = document.querySelector(${JSON.stringify(scrollSelector)}); if (!target) return false; target.scrollIntoView({block: "center"}); return true; })()`,
          returnByValue: true,
        });
        found = Boolean(scrolled.result?.value);
        if (found) break;
        await sleep(100);
      }
      if (!found) throw new Error(`Capture scroll target missing: ${scrollSelector}`);
    }
  }
  const settleMs = Number(process.env.CUTROOM_CAPTURE_SETTLE_MS || 500);
  if (!Number.isFinite(settleMs) || settleMs < 0 || settleMs > 10_000) {
    throw new Error("Invalid capture settle time");
  }
  await sleep(settleMs);
  const finalMetrics = await command("Runtime.evaluate", {
    expression: "JSON.stringify({width:innerWidth,scrollWidth:document.documentElement.scrollWidth})",
    returnByValue: true,
  });
  const finalState = JSON.parse(finalMetrics.result.value);
  let mediaState;
  const mediaStateSelector = process.env.CUTROOM_CAPTURE_MEDIA_STATE_SELECTOR;
  if (mediaStateSelector) {
    const observed = await command("Runtime.evaluate", {
      expression: `(() => { const media = document.querySelector(${JSON.stringify(mediaStateSelector)}); return media ? JSON.stringify({paused:media.paused,currentTime:media.currentTime,duration:media.duration,readyState:media.readyState,networkState:media.networkState,videoWidth:media.videoWidth,error:media.error?.message || null}) : null; })()`,
      returnByValue: true,
    });
    mediaState = observed.result?.value ? JSON.parse(observed.result.value) : null;
  }
  const screenshot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(output, Buffer.from(screenshot.data, "base64"));
  process.stdout.write(JSON.stringify({ output, ...state, finalWidth: finalState.width,
    finalScrollWidth: finalState.scrollWidth, ...(mediaStateSelector ? { mediaState } : {}) }) + "\n");
} finally {
  socket?.close();
  if (child.pid && child.exitCode === null) {
    spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
  }
  await sleep(300);
  if (!resolve(profile).startsWith(profileRoot + sep)) {
    throw new Error("Refusing to remove Chrome profile outside its output directory");
  }
  try {
    rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  } catch (error) {
    process.stderr.write(`Chrome profile cleanup deferred: ${error.message}\n`);
  }
}
