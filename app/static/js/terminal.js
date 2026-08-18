/* A deliberately small ANSI terminal renderer, written for HomeCloud.
 *
 * It keeps a bounded screen buffer and understands the control sequences a
 * normal Linux serial shell emits: CR/LF, backspace, tab, SGR colours, erase,
 * cursor movement, save and restore. It is not a dependency or an iframe.
 */
(function () {
  "use strict";

  const COLOURS = [
    "#d86f45", "#73a6d4", "#79a982", "#dbb45f",
    "#ad82cf", "#6faeb2", "#d8d4c8", "#80796f",
    "#b55038", "#8bbadf", "#8ac494", "#e2c06f",
    "#c69bdf", "#86c9cd", "#f3eee2", "#ffffff",
  ];

  class Terminal {
    constructor(element, maxLines) {
      this.element = element;
      this.maxLines = maxLines || 800;
      this.clear();
    }

    clear() {
      this.lines = [[]];
      this.row = 0;
      this.col = 0;
      this.saved = { row: 0, col: 0 };
      this.style = { fg: null, bg: null, bold: false };
      this.mode = "text";
      this.params = "";
      this.render();
    }

    write(text) {
      for (const char of text) this.consume(char);
      this.render();
    }

    consume(char) {
      if (this.mode === "esc") {
        if (char === "[") {
          this.mode = "csi";
          this.params = "";
        } else if (char === "]") {
          this.mode = "osc";
        } else {
          this.mode = "text";
        }
        return;
      }
      if (this.mode === "osc") {
        if (char === "\u0007") this.mode = "text";
        if (char === "\u001b") this.mode = "osc-esc";
        return;
      }
      if (this.mode === "osc-esc") {
        this.mode = char === "\\" ? "text" : "osc";
        return;
      }
      if (this.mode === "csi") {
        this.params += char;
        if (char >= "@" && char <= "~") {
          this.handleCsi(char, this.params.slice(0, -1));
          this.mode = "text";
        }
        return;
      }

      if (char === "\u001b") { this.mode = "esc"; return; }
      if (char === "\r") { this.col = 0; return; }
      if (char === "\n") { this.row += 1; this.ensureRow(); return; }
      if (char === "\b" || char === "\u007f") { this.col = Math.max(0, this.col - 1); return; }
      if (char === "\t") {
        const count = 8 - (this.col % 8);
        for (let index = 0; index < count; index += 1) this.put(" ");
        return;
      }
      if (char >= " ") this.put(char);
    }

    values(raw) {
      return raw.replace(/^\?/, "").split(";").map((value) => Number(value || 0));
    }

    handleCsi(command, raw) {
      const values = this.values(raw);
      const first = values[0] || 1;
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
      } else if (command === "A") this.row = Math.max(0, this.row - first);
      else if (command === "B") { this.row += first; this.ensureRow(); }
      else if (command === "C") this.col += first;
      else if (command === "D") this.col = Math.max(0, this.col - first);
      else if (command === "G") this.col = Math.max(0, first - 1);
      else if (command === "H" || command === "f") {
        this.row = Math.max(0, (values[0] || 1) - 1);
        this.col = Math.max(0, (values[1] || 1) - 1);
        this.ensureRow();
      } else if (command === "J") {
        if ((values[0] || 0) === 2 || (values[0] || 0) === 3) this.clear();
        else this.lines.splice(this.row + 1);
      } else if (command === "K") {
        this.lines[this.row].splice(this.col);
      } else if (command === "s") {
        this.saved = { row: this.row, col: this.col };
      } else if (command === "u") {
        this.row = this.saved.row;
        this.col = this.saved.col;
      }
    }

    put(char) {
      this.ensureRow();
      const line = this.lines[this.row];
      while (line.length < this.col) line.push({ char: " ", style: {} });
      line[this.col] = { char, style: { ...this.style } };
      this.col += 1;
    }

    ensureRow() {
      while (this.lines.length <= this.row) this.lines.push([]);
      while (this.lines.length > this.maxLines) {
        this.lines.shift();
        this.row = Math.max(0, this.row - 1);
      }
    }

    render() {
      const fragment = document.createDocumentFragment();
      this.lines.forEach((line, row) => {
        const lineEl = document.createElement("div");
        lineEl.className = "terminal-line";
        let run = "";
        let styleKey = "";
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
          const isCursor = row === this.row && col === this.col;
          const key = JSON.stringify(cell.style || {});
          if (isCursor || key !== styleKey) {
            flush();
            styleKey = key;
            runStyle = cell.style;
          }
          run += cell.char;
        });
        if (row === this.row && this.col >= line.length) {
          flush();
          const cursor = document.createElement("span");
          cursor.className = "terminal-cursor";
          cursor.textContent = " ";
          lineEl.appendChild(cursor);
        } else {
          flush();
        }
        fragment.appendChild(lineEl);
      });
      this.element.replaceChildren(fragment);
    }
  }

  window.HCTerminal = Terminal;
})();

