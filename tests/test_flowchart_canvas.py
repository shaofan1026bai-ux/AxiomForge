"""预览协议去重：普通props更新不能再次启动同一轮Mermaid渲染。"""

import shutil
import subprocess
from pathlib import Path

import pytest

from axiomforge.ui.flowchart_canvas import FlowchartCanvas


def test_dirty_prop_only_updates_on_change():
    canvas = FlowchartCanvas.__new__(FlowchartCanvas)
    canvas._props = {"dirty": False}
    updates = []
    canvas.update = lambda: updates.append(True)
    canvas.set_dirty(False)
    canvas.set_dirty(True)
    canvas.set_dirty(True)
    assert updates == [True]


def test_same_preview_ticket_is_queued_once_and_invalidations_cancel_old_ticket():
    node = shutil.which("node")
    if not node:
        pytest.skip("JS协议验证需要Node；实际画布另由浏览器验收。")
    source = Path("src/axiomforge/ui/flowchart_canvas.js").resolve()
    # 仅测试组件协议与队列，不以模拟DOM冒充真实Mermaid排版验证。
    script = r"""
      const fs = require('node:fs');
      const vm = require('node:vm');
      const assert = require('node:assert/strict');
      let source = fs.readFileSync(process.argv[1], 'utf8');
      source = source.replace('import { mermaid } from "nicegui-mermaid";', 'const mermaid = {};');
      source = source.replace('export default {', 'globalThis.component = {');
      const context = vm.createContext({});
      vm.runInContext(source, context);
      const viewport = {clientWidth:700, clientHeight:660};
      const geometry = {hasDiagram:true, width:900, height:1500, $refs:{viewport}};
      context.component.methods.readable.call(geometry);
      assert.equal(geometry.scale, 0.8);
      assert.equal(geometry.y, 24);
      context.component.methods.fit.call(geometry);
      assert.equal(geometry.scale, (660 - 48) / 1500);
      const calls = [];
      const instance = {alive:true, request:{ticket:1, source:'flowchart TD\n A-->B'}};
      instance.isCurrent = context.component.methods.isCurrent;
      instance.render = async request => {
        if (instance.isCurrent(request)) calls.push(request.ticket);
      };
      const enqueue = context.component.methods.enqueue.bind(instance);
      (async () => {
        enqueue(instance.request);
        enqueue({...instance.request});
        await vm.runInContext('queue', context);
        assert.deepEqual(calls, [1]);
        instance.request = {ticket:2, source:'flowchart TD\n A-->C'};
        enqueue(instance.request);
        instance.request = {ticket:3};
        enqueue(instance.request);
        await vm.runInContext('queue', context);
        assert.deepEqual(calls, [1]);
        instance.request = {ticket:4, source:'flowchart TD\n A-->D'};
        enqueue(instance.request);
        await vm.runInContext('queue', context);
        assert.deepEqual(calls, [1, 4]);
      })().catch(error => {console.error(error);process.exitCode=1;});
    """
    result = subprocess.run(
        [node, "-e", script, str(source)], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
