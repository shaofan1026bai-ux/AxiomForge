import { mermaid } from "nicegui-mermaid";

// Mermaid配置为模块级状态；串行解析及渲染，禁止主题在两张图之间交错。
let queue = Promise.resolve();
let sequence = 0;
const SVG_NS = "http://www.w3.org/2000/svg";
const SAFE_TAGS = new Set(["svg", "g", "defs", "style", "title", "desc", "path", "rect", "polygon", "polyline", "circle", "ellipse", "line", "text", "tspan", "marker", "clippath", "filter", "feflood", "fecomposite", "fegaussianblur", "feoffset", "femerge", "femergenode", "fedropshadow", "fecolormatrix", "feblend", "fecomponenttransfer", "fefuncr", "fefuncg", "fefuncb", "fefunca", "femorphology", "feturbulence", "fedisplacementmap", "lineargradient", "radialgradient", "stop", "symbol", "use", "view", "pattern", "mask"]);

function cleanSvg(markup) {
  if (typeof markup !== "string" || markup.length > 2000000 || /<!DOCTYPE|<!ENTITY/i.test(markup)) throw new Error("SVG内容不符合展示要求。");
  const document = new DOMParser().parseFromString(markup, "image/svg+xml");
  if (document.querySelector("parsererror") || document.documentElement.localName !== "svg") throw new Error("SVG内容无法解析。");
  for (const element of document.querySelectorAll("*")) {
    if (element.namespaceURI !== SVG_NS || !SAFE_TAGS.has(element.localName.toLowerCase())) throw new Error("SVG包含不支持的元素：" + element.localName + "（" + element.namespaceURI + "）。");
    for (const attr of Array.from(element.attributes)) {
      const name = attr.localName.toLowerCase();
      if (name.startsWith("on") || ["src", "srcset"].includes(name)) throw new Error("SVG包含不支持的活动属性。");
      if (name === "href" && !/^#[A-Za-z_][\w:.-]*$/.test(attr.value)) throw new Error("SVG不能包含外部资源。");
      if (/javascript\s*:|data\s*:|@import|expression\s*\(/i.test(attr.value)) throw new Error("SVG不能包含脚本或外部资源。");
      for (const match of attr.value.matchAll(/url\s*\(([^)]*)\)/gi)) {
        if (!/^['"]?#[A-Za-z_][\w:.-]*['"]?$/.test(match[1].trim())) throw new Error("SVG不能包含外部资源。");
      }
    }
    if (element.hasAttribute("style")) {
      for (const property of Array.from(element.style)) {
        if (/^(animation|transition)(-|$)/i.test(property)) element.style.removeProperty(property);
      }
    }
    if (element.localName === "style") {
      if (/@import|javascript\s*:|data\s*:|expression\s*\(/i.test(element.textContent)) throw new Error("SVG样式含外部资源。");
      for (const match of element.textContent.matchAll(/url\s*\(([^)]*)\)/gi)) {
        if (!/^['"]?#[A-Za-z_][\w:.-]*['"]?$/.test(match[1].trim())) throw new Error("SVG样式含外部资源。");
      }
      // 去掉引擎默认但本模块不使用的动画，不改变静态classDef视觉。
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(element.textContent);
      const rules = [];
      for (const rule of sheet.cssRules) {
        if (rule.type !== CSSRule.STYLE_RULE) continue;
        for (const property of Array.from(rule.style)) {
          if (/^(animation|transition)(-|$)/.test(property)) rule.style.removeProperty(property);
        }
        rules.push(rule.cssText);
      }
      element.textContent = rules.join("\n");
    }
  }
  return document.documentElement;
}

export default {
  template: `<div class="flowchart-canvas">
    <div class="flowchart-canvas-tools">
      <button type="button" aria-label="缩小流程图" @click="zoomBy(1 / 1.2)">−</button>
      <span class="flowchart-zoom-label" aria-live="polite">{{ Math.round(scale * 100) }}%</span>
      <button type="button" aria-label="放大流程图" @click="zoomBy(1.2)">＋</button>
      <button type="button" @click="fit">适应窗口</button>
      <button type="button" @click="fullscreen">{{ full ? '退出全屏' : '全屏' }}</button>
      <span class="flowchart-canvas-hint">滚轮缩放 · 拖动空白处平移</span>
    </div>
    <div ref="viewport" class="flowchart-viewport" role="region" aria-label="算法流程图画布" tabindex="0"
      @wheel.prevent="wheel" @pointerdown="pointerDown" @pointermove="pointerMove" @pointerup="pointerUp" @pointercancel="pointerUp">
      <div ref="diagram" class="flowchart-diagram" :style="{transform: 'translate(' + x + 'px,' + y + 'px) scale(' + scale + ')'}"></div>
      <div v-if="!hasDiagram" class="flowchart-canvas-empty">点击更新预览，查看流程图</div>
    </div>
  </div>`,
  props: { request: Object, dirty: Boolean },
  data() {
    return { scale: 1, x: 0, y: 0, width: 0, height: 0, hasDiagram: false, full: false, selected: "" };
  },
  mounted() {
    this.alive = true;
    this.currentNodes = [];
    this.panning = null;
    this.beforeUnload = event => { if (this.dirty) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", this.beforeUnload);
    this.fullscreenListener = () => { this.full = document.fullscreenElement === this.$el; };
    document.addEventListener("fullscreenchange", this.fullscreenListener);
    this.observer = new ResizeObserver(() => {
      if (this.needsFit && this.$refs.viewport.clientWidth) {
        if (this.needsFit === "readable") this.readable(); else this.fit();
      }
    });
    this.observer.observe(this.$refs.viewport);
    this.enqueue(this.request);
  },
  beforeUnmount() {
    this.alive = false;
    this.observer?.disconnect();
    window.removeEventListener("beforeunload", this.beforeUnload);
    document.removeEventListener("fullscreenchange", this.fullscreenListener);
  },
  watch: { request: { deep: true, handler(value) { this.enqueue(value); } } },
  methods: {
    isCurrent(request) { return this.alive && this.request?.ticket === request.ticket; },
    enqueue(request) {
      if (!request || !this.alive || request.ticket === this.lastQueuedTicket) return;
      this.lastQueuedTicket = request.ticket;
      if (!request.source) return;
      queue = queue.then(() => this.render(request), () => this.render(request));
    },
    async render(request) {
      if (!this.isCurrent(request)) return;
      const temporary = document.createElement("div");
      const svgId = `af_flow_${this.$el.id}_${++sequence}`;
      temporary.style.cssText = "position:absolute;left:-100000px;top:0;visibility:hidden;pointer-events:none";
      document.body.appendChild(temporary);
      const deadline = performance.now() + 8000;
      let expired = false;
      // 定时器仅提示超时，不宣称能中断同步布局；队列仍等待旧操作结束，避免污染全局配置。
      const timer = setTimeout(() => {
        expired = true;
        if (this.isCurrent(request)) this.$emit("render_error", { ticket: request.ticket, hash: request.hash, message: "流程图渲染超时，请缩小图示后重试。" });
      }, 8000);
      const timed = async promise => {
        const value = await promise;
        if (expired || performance.now() > deadline) throw new Error("流程图渲染超时，请缩小图示后重试。");
        return value;
      };
      try {
        if (new TextEncoder().encode(request.source).length > 65536) throw new Error("Mermaid源码最多64KiB。");
        mermaid.initialize({
          startOnLoad: false, securityLevel: "strict", suppressErrorRendering: true,
          theme: "base", deterministicIds: false, htmlLabels: false, maxTextSize: 65536, maxEdges: 200,
          fontFamily: '"PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
          themeVariables: request.dark
            ? { background: "#162132", primaryColor: "#1e2d43", primaryTextColor: "#e5eaf3", primaryBorderColor: "#8090aa", lineColor: "#a1afc4", secondaryColor: "#24344c", tertiaryColor: "#162132", edgeLabelBackground: "#162132", fontSize: "16px" }
            : { background: "#ffffff", primaryColor: "#eef2ff", primaryTextColor: "#192337", primaryBorderColor: "#7c8ba3", lineColor: "#64748b", secondaryColor: "#f0f4fa", tertiaryColor: "#ffffff", edgeLabelBackground: "#ffffff", fontSize: "16px" },
          flowchart: { htmlLabels: false, curve: "linear", padding: 16, nodeSpacing: 36, rankSpacing: 44, wrappingWidth: 220 },
        });
        const parsed = await timed(mermaid.mermaidAPI.getDiagramFromText(request.source));
        if (!/^flowchart/.test(parsed.type) || !parsed.db.getVertices || !parsed.db.getEdges) throw new Error("首版仅支持Mermaid流程图。");
        const vertices = parsed.db.getVertices();
        const entries = vertices instanceof Map ? Array.from(vertices.values()) : Object.values(vertices);
        if (entries.length > 100 || parsed.db.getEdges().length > 200) throw new Error("首版最多100个节点、200条连线。");
        const nodes = entries.map(vertex => ({ id: vertex.id, label: String(vertex.text || vertex.id), type: ["diamond", "question"].includes(vertex.type) ? "判断" : "动作／步骤", configurable: (vertex.classes || []).includes("configurable"), domId: vertex.domId }));
        if (!this.isCurrent(request)) return;
        const result = await timed(mermaid.render(svgId, request.source, temporary));
        if (!this.isCurrent(request)) return;
        const svg = cleanSvg(result.svg);
        this.pending = { ticket: request.ticket, hash: request.hash, nodes };
        this.$emit("rendered", { ticket: request.ticket, hash: request.hash, svg: new XMLSerializer().serializeToString(svg), nodes });
      } catch (error) {
        if (this.isCurrent(request)) this.$emit("render_error", { ticket: request.ticket, hash: request.hash, message: String(error.message || "源码无法解析").slice(0, 600) });
      } finally {
        clearTimeout(timer);
        temporary.remove();
      }
    },
    publish(ticket, markup, nodes, preserve) {
      if (!this.pending || ticket !== this.pending.ticket || ticket !== this.request?.ticket || !this.alive) return;
      try {
        const svg = cleanSvg(markup);
        const old = { width: this.width, height: this.height, x: this.x, y: this.y, scale: this.scale };
        const box = svg.viewBox.baseVal;
        this.width = box.width || parseFloat(svg.getAttribute("width")) || 800;
        this.height = box.height || parseFloat(svg.getAttribute("height")) || 600;
        svg.setAttribute("width", String(this.width));
        svg.setAttribute("height", String(this.height));
        svg.style.maxWidth = "none";
        svg.style.width = this.width + "px";
        svg.style.height = this.height + "px";
        const imported = document.importNode(svg, true);
        this.$refs.diagram.replaceChildren(imported);
        this.hasDiagram = true;
        this.currentNodes = nodes;
        this.publishedHash = this.pending.hash;
        this.publishedTicket = ticket;
        for (const node of nodes) {
          const element = Array.from(imported.querySelectorAll("g.node")).find(element => element.id === node.domId || element.id.endsWith("-" + node.domId));
          if (!element) continue;
          element.dataset.flowNode = node.id;
          element.setAttribute("tabindex", "0");
          element.setAttribute("role", "button");
          element.setAttribute("aria-label", node.label + (node.configurable ? "，可配置条件" : ""));
          const select = () => { this.selectNode(node.id); this.$emit("node_selected", { id: node.id, hash: this.publishedHash, ticket: this.publishedTicket }); };
          element.addEventListener("click", select);
          element.addEventListener("keydown", event => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); select(); } });
        }
        this.selectNode(this.selected);
        if (preserve && old.width && old.height) {
          const viewport = this.$refs.viewport;
          const cx = (viewport.clientWidth / 2 - old.x) / old.scale / old.width;
          const cy = (viewport.clientHeight / 2 - old.y) / old.scale / old.height;
          this.scale = old.scale;
          this.x = viewport.clientWidth / 2 - cx * this.width * this.scale;
          this.y = viewport.clientHeight / 2 - cy * this.height * this.scale;
        } else this.readable();
      } catch (error) {
        this.$emit("render_error", { ticket, hash: this.pending.hash, message: String(error.message).slice(0, 600) });
      }
    },
    selectNode(id) {
      this.selected = id;
      for (const node of this.$refs.diagram.querySelectorAll("g.node")) node.classList.toggle("flow-node-selected", node.dataset.flowNode === id);
    },
    readable() {
      if (!this.hasDiagram) return;
      const viewport = this.$refs.viewport;
      if (!viewport.clientWidth || !viewport.clientHeight) { this.needsFit = "readable"; return; }
      this.needsFit = false;
      // 初次打开优先保证中文可读；长竖图从顶部开始，整图概览由用户显式选择。
      this.scale = Math.max(0.8, Math.min(1, (viewport.clientWidth - 48) / this.width));
      this.x = (viewport.clientWidth - this.width * this.scale) / 2;
      this.y = 24;
    },
    fit() {
      if (!this.hasDiagram) return;
      const viewport = this.$refs.viewport;
      if (!viewport.clientWidth || !viewport.clientHeight) { this.needsFit = true; return; }
      this.needsFit = false;
      this.scale = Math.max(0.25, Math.min(2, (viewport.clientWidth - 48) / this.width, (viewport.clientHeight - 48) / this.height));
      this.x = (viewport.clientWidth - this.width * this.scale) / 2;
      this.y = (viewport.clientHeight - this.height * this.scale) / 2;
    },
    zoomBy(factor, px, py) {
      if (!this.hasDiagram) return;
      const viewport = this.$refs.viewport;
      px ??= viewport.clientWidth / 2;
      py ??= viewport.clientHeight / 2;
      const next = Math.max(0.25, Math.min(2, this.scale * factor));
      this.x = px - (px - this.x) * next / this.scale;
      this.y = py - (py - this.y) * next / this.scale;
      this.scale = next;
    },
    wheel(event) { const box = this.$refs.viewport.getBoundingClientRect(); this.zoomBy(event.deltaY < 0 ? 1.12 : 1 / 1.12, event.clientX - box.left, event.clientY - box.top); },
    pointerDown(event) {
      if (event.button !== 0 || event.target.closest("g.node")) return;
      this.panning = { px: event.clientX, py: event.clientY, x: this.x, y: this.y };
      this.$refs.viewport.setPointerCapture(event.pointerId);
    },
    pointerMove(event) { if (this.panning) { this.x = this.panning.x + event.clientX - this.panning.px; this.y = this.panning.y + event.clientY - this.panning.py; } },
    pointerUp() { this.panning = null; },
    async fullscreen() {
      try { if (document.fullscreenElement === this.$el) await document.exitFullscreen(); else await this.$el.requestFullscreen(); }
      catch { this.$emit("render_error", { ticket: this.request?.ticket, hash: this.publishedHash, message: "当前浏览器无法进入全屏，画布仍可正常查看。" }); }
    },
  },
};
