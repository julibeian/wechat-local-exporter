"""Exercise the script registration/load ordering that browsers actually use."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_previous_load_event_cannot_reject_new_request_for_same_chunk(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the viewer loader event-order regression")
    source = Path(__file__).resolve().parents[1] / "src/wechat_exporter/html_assets/viewer.js"
    harness = tmp_path / "loader.cjs"
    harness.write_text(r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const scripts = [];
const context = {
  window: {},
  document: {
    addEventListener() {},
    createElement() { return {events: {}, addEventListener(name, fn) { this.events[name] = fn; }, remove() {}}; },
    head: {appendChild(script) { scripts.push(script); }}
  }
};
vm.createContext(context);
// Expose only the private loader in the isolated test context; production has no test hook.
const source = fs.readFileSync(process.argv[2], 'utf8');
vm.runInContext(source.replace('  // Native scrolling with measured spacers.',
  '  window.testLoad = loadChunkData;\n  // Native scrolling with measured spacers.'), context);
(async () => {
  const chunk = {id: 'one', path: 'one.js'};
  const first = context.window.testLoad(chunk);
  context.window.WeChatArchive.registerChunk('one', [{id: 1}]);
  assert.equal((await first)[0].id, 1);
  const second = context.window.testLoad(chunk);
  // Old script's load event fires after a continuation began the next request.
  scripts[0].events.load();
  context.window.WeChatArchive.registerChunk('one', [{id: 2}]);
  scripts[1].events.load();
  assert.equal((await second)[0].id, 2);
  const broken = context.window.testLoad(chunk);
  scripts[2].events.load();
  await assert.rejects(broken, /did not register/);
})().catch(error => { console.error(error); process.exitCode = 1; });
''', encoding="utf-8")
    subprocess.run([node, str(harness), str(source)], check=True, capture_output=True, text=True)
