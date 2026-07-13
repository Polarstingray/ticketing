import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Start every E2E run from an empty database so the backend re-seeds the admin
// and assertions don't depend on leftover state from a previous run.
export default function globalSetup() {
  const dataDir = path.join(__dirname, ".data");
  if (fs.existsSync(dataDir)) {
    fs.rmSync(dataDir, { recursive: true, force: true });
  }
  fs.mkdirSync(dataDir, { recursive: true });
}
