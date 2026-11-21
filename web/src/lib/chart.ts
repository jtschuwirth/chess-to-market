export type Point = { x: number; y: number };

export type Series = {
  name: string;
  color: string;
  points: Point[];
  dashed?: boolean;
};

export type Rule = { x: number; label: string; tone?: "neutral" | "warn" | "bad" };

export type ChartOpts = {
  series: Series[];
  rules?: Rule[];
  yDomain?: [number, number];
  yTicks?: number;
  height?: number;
  fmtY?: (n: number) => string;
  fmtX?: (n: number) => string;
  labelEnds?: boolean;
};

const W = 760;
const PAD = { t: 14, r: 56, b: 26, l: 40 };

const svgEl = <K extends keyof SVGElementTagNameMap>(
  name: K,
  attrs: Record<string, string | number> = {},
): SVGElementTagNameMap[K] => {
  const e = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
  return e;
};

const niceStep = (span: number) => {
  const raw = span / 5;
  const mag = 10 ** Math.floor(Math.log10(raw || 1));
  for (const m of [1, 2, 2.5, 5, 10]) if (raw <= m * mag) return m * mag;
  return 10 * mag;
};

export function drawChart(host: HTMLElement, o: ChartOpts): void {
  const H = o.height ?? 260;
  const fmtY = o.fmtY ?? ((n) => n.toFixed(2));
  const fmtX = o.fmtX ?? ((n) => `${Math.round(n)}s`);
  const all = o.series.flatMap((s) => s.points);

  host.textContent = "";
  if (!all.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = "no data yet";
    host.append(p);
    return;
  }

  const xs = all.map((p) => p.x);
  let x0 = Math.min(...xs);
  let x1 = Math.max(...xs);
  if (x1 - x0 < 1e-9) {
    x0 -= 1;
    x1 += 1;
  }
  const [y0, y1] = o.yDomain ?? [
    Math.min(...all.map((p) => p.y)),
    Math.max(...all.map((p) => p.y)),
  ];
  const yLo = y0 === y1 ? y0 - 0.5 : y0;
  const yHi = y0 === y1 ? y1 + 0.5 : y1;

  const px = (x: number) => PAD.l + ((x - x0) / (x1 - x0)) * (W - PAD.l - PAD.r);
  const py = (y: number) => H - PAD.b - ((y - yLo) / (yHi - yLo)) * (H - PAD.t - PAD.b);

  const svg = svgEl("svg", {
    viewBox: `0 0 ${W} ${H}`,
    role: "img",
    "aria-label": o.series.map((s) => s.name).join(", "),
  });

  const step = niceStep(yHi - yLo);
  for (let v = Math.ceil(yLo / step) * step; v <= yHi + 1e-9; v += step) {
    svg.append(
      svgEl("line", {
        x1: PAD.l,
        x2: W - PAD.r,
        y1: py(v),
        y2: py(v),
        stroke: "var(--grid)",
        "stroke-width": 1,
      }),
    );
    const t = svgEl("text", {
      x: PAD.l - 7,
      y: py(v) + 3.5,
      "text-anchor": "end",
      fill: "var(--text-muted)",
      "font-size": 10,
    });
    t.textContent = fmtY(v);
    svg.append(t);
  }

  const xStep = niceStep(x1 - x0);
  for (let v = Math.ceil(x0 / xStep) * xStep; v <= x1 + 1e-9; v += xStep) {
    const t = svgEl("text", {
      x: px(v),
      y: H - PAD.b + 14,
      "text-anchor": "middle",
      fill: "var(--text-muted)",
      "font-size": 10,
    });
    t.textContent = fmtX(v);
    svg.append(t);
  }

  for (const r of o.rules ?? []) {
    if (r.x < x0 || r.x > x1) continue;
    const stroke =
      r.tone === "bad" ? "var(--critical)" : r.tone === "warn" ? "var(--warning)" : "var(--border-strong)";
    svg.append(
      svgEl("line", {
        x1: px(r.x),
        x2: px(r.x),
        y1: PAD.t,
        y2: H - PAD.b,
        stroke,
        "stroke-width": 1.5,
        "stroke-dasharray": "3 3",
      }),
    );
    const t = svgEl("text", {
      x: px(r.x) + 4,
      y: PAD.t + 9,
      fill: stroke,
      "font-size": 9.5,
    });
    t.textContent = r.label;
    svg.append(t);
  }

  for (const s of o.series) {
    if (!s.points.length) continue;
    const d = s.points
      .map((p, i) => `${i ? "L" : "M"}${px(p.x).toFixed(2)} ${py(p.y).toFixed(2)}`)
      .join(" ");
    svg.append(
      svgEl("path", {
        d,
        fill: "none",
        stroke: s.color,
        "stroke-width": 2,
        "stroke-linejoin": "round",
        "stroke-linecap": "round",
        ...(s.dashed ? { "stroke-dasharray": "5 4" } : {}),
      }),
    );
    if (s.points.length === 1) {
      const p = s.points[0];
      svg.append(
        svgEl("circle", {
          cx: px(p.x),
          cy: py(p.y),
          r: 4,
          fill: s.color,
          stroke: "var(--surface-1)",
          "stroke-width": 2,
        }),
      );
    }
    if (o.labelEnds !== false) {
      const last = s.points[s.points.length - 1];
      const t = svgEl("text", {
        x: W - PAD.r + 7,
        y: py(last.y) + 3.5,
        fill: s.color,
        "font-size": 10.5,
      });
      t.textContent = s.name;
      svg.append(t);
    }
  }

  const cross = svgEl("line", {
    y1: PAD.t,
    y2: H - PAD.b,
    stroke: "var(--border-strong)",
    "stroke-width": 1,
    opacity: 0,
  });
  svg.append(cross);
  const dots = o.series.map((s) =>
    svgEl("circle", {
      r: 4.5,
      fill: s.color,
      stroke: "var(--surface-1)",
      "stroke-width": 2,
      opacity: 0,
    }),
  );
  dots.forEach((d) => svg.append(d));

  host.append(svg);

  const tip = document.createElement("div");
  tip.className = "tip";
  host.append(tip);

  const nearest = (s: Series, x: number) => {
    let best = s.points[0];
    for (const p of s.points) if (Math.abs(p.x - x) < Math.abs(best.x - x)) best = p;
    return best;
  };

  const move = (ev: PointerEvent) => {
    const box = svg.getBoundingClientRect();
    const sx = ((ev.clientX - box.left) / box.width) * W;
    if (sx < PAD.l - 4 || sx > W - PAD.r + 4) return leave();
    const x = x0 + ((sx - PAD.l) / (W - PAD.l - PAD.r)) * (x1 - x0);
    cross.setAttribute("x1", String(sx));
    cross.setAttribute("x2", String(sx));
    cross.setAttribute("opacity", "1");

    let head = fmtX(x);
    const rows: string[] = [];
    o.series.forEach((s, i) => {
      const d = dots[i];
      if (!s.points.length) {
        d.setAttribute("opacity", "0");
        return;
      }
      const p = nearest(s, x);
      d.setAttribute("cx", String(px(p.x)));
      d.setAttribute("cy", String(py(p.y)));
      d.setAttribute("opacity", "1");
      head = fmtX(p.x);
      rows.push(
        `<div class="row"><span class="name" style="color:${s.color}"><i></i>${s.name}</span><span>${fmtY(p.y)}</span></div>`,
      );
    });
    tip.innerHTML = `<div class="head">${head}</div>${rows.join("")}`;
    tip.classList.add("on");
    const left = (sx / W) * host.clientWidth;
    tip.style.left = `${Math.min(Math.max(left + 12, 4), host.clientWidth - tip.offsetWidth - 4)}px`;
    tip.style.top = `8px`;
  };

  const leave = () => {
    cross.setAttribute("opacity", "0");
    dots.forEach((d) => d.setAttribute("opacity", "0"));
    tip.classList.remove("on");
  };

  svg.addEventListener("pointermove", move);
  svg.addEventListener("pointerleave", leave);
}
