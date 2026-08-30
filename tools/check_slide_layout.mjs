import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const chrome = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const slideFile = new URL("../interactive-slides.html", import.meta.url);
const viewports = [
  { width: 1366, height: 768 },
  { width: 960, height: 700 },
];

async function waitForDevToolsPort(profile) {
  const activePort = join(profile, "DevToolsActivePort");
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const [port] = (await readFile(activePort, "utf8")).split(/\r?\n/);
      return port;
    } catch {
      await new Promise(resolve => setTimeout(resolve, 50));
    }
  }
  throw new Error("Chrome DevTools port was not created.");
}

async function evaluatePage(webSocketUrl, expression) {
  const socket = new WebSocket(webSocketUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });

  let requestId = 0;
  const pending = new Map();
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(message.error.message));
    else resolve(message.result);
  });

  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      requestId += 1;
      pending.set(requestId, { resolve, reject });
      socket.send(JSON.stringify({ id: requestId, method, params }));
    });

  await send("Runtime.enable");
  await new Promise(resolve => setTimeout(resolve, 1200));
  const result = await send("Runtime.evaluate", {
    expression,
    returnByValue: true,
  });
  socket.close();
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.text);
  }
  return result.result.value;
}

async function inspectViewport({ width, height }) {
  const profile = await mkdtemp(join(tmpdir(), "slide-layout-"));
  const url = `${slideFile.href}#3`;
  const process = spawn(
    chrome,
    [
      "--headless=new",
      "--disable-gpu",
      "--allow-file-access-from-files",
      "--remote-debugging-port=0",
      `--user-data-dir=${profile}`,
      `--window-size=${width},${height}`,
      "--force-device-scale-factor=1",
      url,
    ],
    { stdio: "ignore" },
  );

  try {
    const port = await waitForDevToolsPort(profile);
    const targets = await fetch(`http://127.0.0.1:${port}/json/list`).then(response =>
      response.json(),
    );
    const page = targets.find(target => target.type === "page");
    if (!page) throw new Error("Chrome page target was not found.");

    return await evaluatePage(
      page.webSocketDebuggerUrl,
      `(() => {
        const slide = document.querySelectorAll(".slide")[2];
        const title = slide.querySelector("h2");
        const content = slide.querySelector(".content");
        const qr = document.querySelector(".qr-badge");
        const rect = element => {
          const value = element.getBoundingClientRect();
          return { left: value.left, top: value.top, right: value.right, bottom: value.bottom };
        };
        const overlaps = (a, b) =>
          a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
        const titleRect = rect(title);
        const contentRect = rect(content);
        const qrRect = rect(qr);
        const qrTextOverlaps = [...slide.querySelectorAll("h2,h3,p,b,strong,span,small")]
          .filter(element => {
            const style = getComputedStyle(element);
            const value = rect(element);
            return style.visibility !== "hidden" &&
              style.display !== "none" &&
              value.right > value.left &&
              value.bottom > value.top &&
              overlaps(value, qrRect);
          })
          .map(element => element.textContent.trim().replace(/\\s+/g, " ").slice(0, 80));
        return {
          viewport: { width: innerWidth, height: innerHeight },
          titleClearance: contentRect.top - titleRect.bottom,
          titleCovered: contentRect.top - titleRect.bottom < 24,
          titleRect,
          contentRect,
          qrTextOverlaps,
        };
      })()`,
    );
  } finally {
    await new Promise(resolve => {
      process.once("exit", resolve);
      process.kill();
      setTimeout(resolve, 1000);
    });
    for (let attempt = 0; attempt < 12; attempt += 1) {
      try {
        await rm(profile, { recursive: true, force: true });
        break;
      } catch (error) {
        if (attempt === 11) {
          console.warn(`Could not remove temporary Chrome profile: ${error.code}`);
          break;
        }
        await new Promise(resolve => setTimeout(resolve, 250));
      }
    }
  }
}

const results = [];
for (const viewport of viewports) {
  results.push(await inspectViewport(viewport));
}

console.log(JSON.stringify(results, null, 2));
if (results.some(result => result.titleCovered || result.qrTextOverlaps.length > 0)) {
  process.exitCode = 1;
}
