/* A compact, dependency-free terminal inspired by xterm.js' integration API.
 *
 * It deliberately implements the useful serial-console subset: a bounded
 * scrollback buffer, streamed writes, ANSI colours/cursor controls, automatic
 * fitting, and an onData event for the transport.  It is not a full terminal
 * emulator (there is no guest PTY behind Firecracker's serial port).
 */
(function () {
  "use strict";

  const COLOURS = [
    "#d86f45", "#73a6d4", "#79a982", "#dbb45f",
    "#ad82cf", "#6faeb2", "#d8d4c8", "#80796f",
    "#b55038", "#8bbadf", "#8ac494", "#e2c06f",
    "#c69bdf", "#86c9cd", "#f3eee2", "#ffffff",
  ];

  function sameStyle(left, right) {
    return left && right && left.fg === right.fg && left.bg === right.bg && left.bold === right.bold;
  }

  class Terminal {
    constructor(element, options) {
      this.element = element;
      this.options = Object.assign({ scrollback: 800 }, options || {});
      this.dataListeners = new Set();
      this.resizeListeners = new Set();
      this.writeQueue = [];
      this.renderFrame = null;
      this.resizeObserver = new ResizeObserver(() => this.fit());
      this.element.classList.add("hc-terminal");
      this.element.addEventListener("keydown", (event) => this.handleKey(event));
      this.element.addEventListener("paste", (event) => this.handlePaste(event));
      this.resizeObserver.observe(this.element);
      this.resetBuffer();
      this.fit();
    }

    resetBuffer() {
      this.lines = [[]];
      this.row = 0;
      this.col = 0;
      this.saved = { row: 0, col: 0 };
      this.style = { fg: null, bg: null, bold: false };
      this.cursorVisible = true;
      this.mode = "text";
      this.params = "";
      this.dirtyFrom = 0;
    }

    clear() {
      this.writeQueue = [];
      this.resetBuffer();
      this.scheduleRender();
    }

    focus() { this.element.focus(); }

    onData(listener) {
      this.dataListeners.add(listener);
      return { dispose: () => this.dataListeners.delete(listener) };
    }

    onResize(listener) {
      this.resizeListeners.add(listener);
      return { dispose: () => this.resizeListeners.delete(listener) };
    }

    write(data) {
      if (data) this.writeQueue.push(String(data));
      this.scheduleRender();
    }

    dispose() {
      this.resizeObserver.disconnect();
      if (this.renderFrame) window.cancelAnimationFrame(this.renderFrame);
      this.dataListeners.clear();
      this.resizeListeners.clear();
    }

    fit() {
      const probe = document.createElement("span");
      probe.className = "terminal-measure";
      probe.textContent = "0000000000";
      this.element.appendChild(probe);
      const rect = probe.getBoundingClientRect();
      probe.remove();
      const style = window.getComputedStyle(this.element);
      const horizontal = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
      const vertical = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
      const nextCols = Math.max(20, Math.floor((this.element.clientWidth - horizontal) / (rect.width / 10 || 8)));
      const nextRows = Math.max(4, Math.floor((this.element.clientHeight - vertical) / (rect.height || 18)));
      if (nextCols === this.cols && nextRows === this.rows) return;
      this.cols = nextCols;
      this.rows = nextRows;
      this.resizeListeners.forEach((listener) => listener({ cols: this.cols, rows: this.rows }));
    }

    scheduleRender() {
      if (this.renderFrame) return;
      this.renderFrame = window.requestAnimationFrame(() => {
        this.renderFrame = null;
        const queue = this.writeQueue;
        this.writeQueue = [];
        for (const text of queue) for (const char of text) this.consume(char);
        this.render();
      });
    }

    markDirty(row) { this.dirtyFrom = Math.min(this.dirtyFrom, Math.max(0, row)); }

    consume(char) {
      if (this.mode === "esc") {
        if (char === "[") { this.mode = "csi"; this.params = ""; }
        else if (char === "]") this.mode = "osc";
        else if (char === "7") { this.saved = { row: this.row, col: this.col }; this.mode = "text"; }
        else if (char === "8") { this.row = this.saved.row; this.col = this.saved.col; this.mode = "text"; }
        else this.mode = "text";
        return;
      }
      if (this.mode === "osc") {
        if (char === "\u0007") this.mode = "text";
        else if (char === "\u001b") this.mode = "osc-esc";
        return;
      }
      if (this.mode === "osc-esc") { this.mode = char === "\\" ? "text" : "osc"; return; }
      if (this.mode === "csi") {
        this.params += char;
        if (char >= "@" && char <= "~") {
          this.handleCsi(char, this.params.slice(0, -1));
          this.mode = "text";
        }
        return;
      }
      if (char === "\u001b") { this.mode = "esc"; return; }
      if (char === "\r") { this.markDirty(this.row); this.col = 0; return; }
      if (char === "\n") { this.markDirty(this.row); this.row += 1; this.ensureRow(); return; }
      if (char === "\b" || char === "\u007f") { this.markDirty(this.row); this.col = Math.max(0, this.col - 1); return; }
      if (char === "\t") {
        const count = 8 - (this.col % 8);
        for (let index = 0; index < count; index += 1) this.put(" ");
        return;
      }
      if (char >= " ") this.put(char);
    }

    values(raw) { return raw.replace(/^\?/, "").split(";").map((value) => Number(value || 0)); }

    handleCsi(command, raw) {
      const privateMode = raw.startsWith("?");
      const values = this.values(raw);
      const first = values[0] || 1;
      this.markDirty(this.row);
      if (command === "m") {
        if (!raw) values[0] = 0;
        for (const value of values) {
          if (value === 0) this.style = { fg: null, bg: null, bold: false };
          else if (value === 1) this.style.bold = true;
          else if (value === 22) this.style.bold = false;
          else if (value >= 30 && value <= 37) this.style.fg = value - 30;
          else if (value >= 90 && value <= 97) this.style.fg = value - 90 + 8;
          else if (value === 39) this.style.fg = null;
          else if (value >= 40 && value <= 47) this.style.bg = value - 40;
          else if (value >= 100 && value <= 107) this.style.bg = value - 100 + 8;
          else if (value === 49) this.style.bg = null;
        }
      } else if (privateMode && (command === "h" || command === "l")) {
        if (values.includes(25)) this.cursorVisible = command === "h";
      } else if (command === "A") this.row = Math.max(0, this.row - first);
      else if (command === "B") { this.row += first; this.ensureRow(); }
      else if (command === "C") this.col = Math.min(this.cols - 1, this.col + first);
      else if (command === "D") this.col = Math.max(0, this.col - first);
      else if (command === "G") this.col = Math.max(0, first - 1);
      else if (command === "H" || command === "f") {
        this.row = Math.max(0, (values[0] || 1) - 1);
        this.col = Math.max(0, (values[1] || 1) - 1);
        this.ensureRow();
      } else if (command === "J") {
        if ((values[0] || 0) === 2 || (values[0] || 0) === 3) this.resetBuffer();
        else this.lines.splice(this.row + 1);
      } else if (command === "K") this.lines[this.row].splice(this.col);
      else if (command === "s") this.saved = { row: this.row, col: this.col };
      else if (command === "u") { this.row = this.saved.row; this.col = this.saved.col; }
    }

    put(char) {
      this.ensureRow();
      const line = this.lines[this.row];
      while (line.length < this.col) line.push({ char: " ", style: {} });
      line[this.col] = { char, style: { ...this.style } };
      this.markDirty(this.row);
      this.col += 1;
      if (this.col >= this.cols) { this.col = 0; this.row += 1; this.ensureRow(); }
    }

    ensureRow() {
      while (this.lines.length <= this.row) this.lines.push([]);
      while (this.lines.length > this.options.scrollback) {
        this.lines.shift();
        this.row = Math.max(0, this.row - 1);
        this.saved.row = Math.max(0, this.saved.row - 1);
        this.dirtyFrom = 0;
      }
    }

    renderLine(line, row) {
      const lineEl = document.createElement("div");
      lineEl.className = "terminal-line";
      let run = "";
      let runStyle = null;
      const flush = () => {
        if (!run) return;
        const span = document.createElement("span");
        span.textContent = run;
        if (runStyle) {
          if (runStyle.fg !== null && runStyle.fg !== undefined) span.style.color = COLOURS[runStyle.fg];
          if (runStyle.bg !== null && runStyle.bg !== undefined) span.style.backgroundColor = COLOURS[runStyle.bg];
          if (runStyle.bold) span.style.fontWeight = "700";
        }
        lineEl.appendChild(span);
        run = "";
      };
      line.forEach((cell, col) => {
        if (!sameStyle(runStyle, cell.style)) { flush(); runStyle = cell.style; }
        run += cell.char;
        if (this.cursorVisible && row === this.row && col + 1 === this.col) {
          flush();
          const cursor = document.createElement("span");
          cursor.className = "terminal-cursor";
          cursor.textContent = " ";
          lineEl.appendChild(cursor);
          runStyle = null;
        }
      });
      flush();
      if (this.cursorVisible && row === this.row && this.col >= line.length) {
        const cursor = document.createElement("span");
        cursor.className = "terminal-cursor";
        cursor.textContent = " ";
        lineEl.appendChild(cursor);
      }
      return lineEl;
    }

    render() {
      const start = Math.max(0, this.dirtyFrom);
      const wasAtBottom = this.element.scrollHeight - this.element.scrollTop - this.element.clientHeight < 24;
      while (this.element.children.length > start) this.element.lastChild.remove();
      const fragment = document.createDocumentFragment();
      for (let row = start; row < this.lines.length; row += 1) fragment.appendChild(this.renderLine(this.lines[row], row));
      this.element.appendChild(fragment);
      this.dirtyFrom = this.lines.length;
      if (wasAtBottom) this.element.scrollTop = this.element.scrollHeight;
    }

    emitData(data) { this.dataListeners.forEach((listener) => listener(data)); }

    handleKey(event) {
      if (event.ctrlKey && event.key.length === 1) {
        const code = event.key.toUpperCase().charCodeAt(0);
        if (code >= 64 && code <= 95) { event.preventDefault(); this.emitData(String.fromCharCode(code - 64)); }
        return;
      }
      const special = { Enter: "\r", Backspace: "\u007f", Tab: "\t", Escape: "\u001b", ArrowUp: "\u001b[A", ArrowDown: "\u001b[B", ArrowRight: "\u001b[C", ArrowLeft: "\u001b[D", Home: "\u001b[H", End: "\u001b[F", Delete: "\u001b[3~" };
      const data = special[event.key] || (event.key.length === 1 && !event.metaKey && !event.altKey ? event.key : null);
      if (data) { event.preventDefault(); this.emitData(data); }
    }

    handlePaste(event) {
      const data = event.clipboardData && event.clipboardData.getData("text");
      if (data) { event.preventDefault(); this.emitData(data); }
    }
  }

  window.HCTerminal = Terminal;
})();
