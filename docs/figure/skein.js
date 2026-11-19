/**
 * Skein: five geese flying in a V, which is what a skein of geese is. The lead
 * at the point is the point assistant; the four down the two arms are the
 * members. The goose under the pointer raises its wings and lifts, and the
 * beat passes down the V goose by goose, smaller the farther it travels. At
 * rest the wings hold a frozen wave and the lead takes the bright stroke.
 * The slider is the stagger, in ms.
 *
 * The pattern: one of many. Tweens, a stagger by distance along the V, and a
 * hit test on the geese's rest centres, which never move.
 */
const {
  Cam, facing, fillet, fit, open, poly, proj, prism, put, rad, rings, solid,
  tdone, tset, tval, tween, disposer, mk, pointer, register,
} = HL;

// Heading: away from the viewer and a little right, so the V opens toward us. F is forward, L the goose's right.
const HEAD = rad(-122), F = [Math.cos(HEAD), Math.sin(HEAD)], L = [-F[1], F[0]];
const UP = 58, LIFT = 8, ROOT = 4.2, WRIST = 14, T = 0.9, PASS = 0.45, PICK = 40;

// The flock in the lead's frame: u forward, v to the right. v < 0 trails on the far side.
const FLOCK = [
  { name: "point", arm: 0, k: 0, u: 0, v: 0, z: 0, th: 24 },
  { name: "muse", arm: -1, k: 1, u: -74, v: -48, z: -5, th: 40 },
  { name: "grok", arm: -1, k: 2, u: -150, v: -95, z: -12, th: -12 },
  { name: "gemini", arm: 1, k: 1, u: -70, v: 51, z: -8, th: -16 },
  { name: "chatgpt", arm: 1, k: 2, u: -154, v: 100, z: -15, th: 34 },
];

// Torso parts, far to near (the goose flies away from us): [u0, v0, u1, v1, r, b, z0, z1].
const TORSO = [
  [29, -0.9, 33.5, 0.9, 0.9, 0.3, -0.2, 0.9], // bill
  [23.5, -2.2, 30, 2.2, 2.2, 0.6, -0.7, 2.0], // head
  [5, -1.4, 25, 1.4, 1.4, 0.45, -0.5, 1.5], // the long outstretched neck of a goose
  [-11, -5, 8, 5, 5, 1.1, -1.8, 2.4], // a plump body
  [-15.5, -3.6, -8.5, 3.6, 3.2, 0.8, -0.3, 0.6], // a short fanned tail
];

// One wing in its own plane: s out along the span, u forward. Broad at the root, a crook at the wrist, a swept tip.
const WING = fillet([[0, 6], [WRIST, 7.5], [27, 3.5], [37, -1.5], [35, -4], [24, -5], [WRIST, -6.5], [0, -7]], [1, 4, 6, 2.4, 2.2, 5, 4, 1]);
const COVERT = [[3, 1], [WRIST, 1.2], [24, -0.4], [31, -2]];

/** Steps along the V between two geese: the way a beat travels. */
const along = (a, b) => (a.arm === b.arm || !a.arm || !b.arm ? Math.abs(a.k - b.k) : a.k + b.k);

function mount({ stage, svg, read }, value) {
  const bag = disposer();
  let stag = value;
  const at = (c, u, v, z) => [c[0] + F[0] * u + L[0] * v, c[1] + F[1] * u + L[1] * v, z];
  const centre = (g) => at([0, 0], g.u, g.v, 0);

  // Fitted to every goose with its wings raised and lifted, so no pose leaves the frame.
  const C = Cam(45, 0.5, 1.2), box = [];
  for (const g of FLOCK) for (const u of [-17, 30]) for (const v of [-41, 41]) for (const z of [g.z - 14, g.z + LIFT + 34]) for (const uu of [u, u + 4]) box.push(at(centre(g), uu, v, z));
  fit(C, box, 200, 166);
  const P = proj(C), front = facing(C);
  const FAR = L[0] + L[1] > 0 ? -1 : 1;
  const turn = (ring, c) => ring.map((q) => ({ ...q, u: c[0] + F[0] * q.u + L[0] * q.v, v: c[1] + F[1] * q.u + L[1] * q.v, nu: F[0] * q.nu + L[0] * q.nv, nv: F[1] * q.nu + L[1] * q.nv }));

  const root = mk("g", {}, svg);
  const wing = (parent) => {
    const g = mk("g", {}, parent);
    return { back: mk("path", { class: "lo" }, g), face: mk("path", { class: "sil" }, g), cov: mk("path", { class: "nf lo" }, g) };
  };
  // Geese from the far one to the nearest; within a goose, both wings, then the torso over their roots.
  const geese = FLOCK.map((f, i) => ({ ...f, i, c: centre(f) }));
  const order = geese.slice().sort((a, b) => a.c[0] + a.c[1] - (b.c[0] + b.c[1]));
  for (const g of order) {
    const gg = mk("g", {}, root);
    g.far = wing(gg);
    g.near = wing(gg);
    g.parts = TORSO.map(([u0, v0, u1, v1, r, b, z0, z1]) => {
      const [ring, inner] = rings(u0, v0, u1, v1, r, b);
      return { ring: turn(ring, g.c), inner: turn(inner, g.c), z0, z1, el: solid(gg) };
    });
    g.hi = [g.far.face, g.near.face, g.parts[3].el.sil];
    g.a = tween(g.th); g.lift = tween(0); g.drawn = "";
    g.rest = P(g.c[0], g.c[1], g.z);
  }

  /** A wing raised th at the shoulder; the hand past the wrist lags a little behind it. */
  function drawWing(w, g, z, sg, th) {
    const a = rad(th), h = rad(th * 0.7 - 7);
    const pt = (s, u, t) => {
      const ang = s <= WRIST ? a : h, out = s <= WRIST ? s * Math.cos(a) : WRIST * Math.cos(a) + (s - WRIST) * Math.cos(h);
      const up = s <= WRIST ? s * Math.sin(a) : WRIST * Math.sin(a) + (s - WRIST) * Math.sin(h);
      // t pushes a point to the wing's underside, for the plate's thickness
      return P(...at(g.c, u, sg * (ROOT + out + t * Math.sin(ang)), z + 0.6 + up - t * Math.cos(ang)));
    };
    w.back.setAttribute("d", poly(WING.map(([s, u]) => pt(s, u, T))));
    w.face.setAttribute("d", poly(WING.map(([s, u]) => pt(s, u, 0))));
    w.cov.setAttribute("d", open(COVERT.map(([s, u]) => pt(s, u, 0))));
  }

  function draw(g, now) {
    const th = tval(g.a, now), lift = tval(g.lift, now), key = th.toFixed(2) + "," + lift.toFixed(2);
    if (key === g.drawn) return;
    g.drawn = key;
    const z = g.z + lift;
    drawWing(g.far, g, z, FAR, th);
    for (const p of g.parts) put(p.el, prism(P, front, p.ring, p.inner, z + p.z0, z + p.z1));
    drawWing(g.near, g, z, -FAR, th);
  }

  const B = register(stage, (_dt, now) => {
    let moving = false;
    for (const g of geese) { draw(g, now); if (!tdone(g.a, now) || !tdone(g.lift, now)) moving = true; }
    return moving;
  });
  bag.add(B.unregister);

  /** The goose whose rest centre is nearest the point, within reach; -1 for none. */
  function hit([x, y]) {
    let best = -1, bd = PICK;
    for (const g of geese) { const d = Math.hypot(x - g.rest[0], y - g.rest[1]); if (d < bd) { bd = d; best = g.i; } }
    return best;
  }

  let act = -1;
  const light = (on) => geese.forEach((g) => g.hi.forEach((el) => el.classList.toggle("hi", g.i === on)));
  /** The chosen goose beats; the beat travels down the V and fades. -1 lets the flock return to its wave. */
  function setActive(a) {
    if (a === act) return;
    const now = performance.now(), from = a >= 0 ? a : act;
    act = a;
    for (const g of geese) {
      const d = along(g, geese[from]), delay = d * stag;
      tset(g.a, a < 0 ? g.th : UP * PASS ** d, now, delay);
      tset(g.lift, a === g.i ? LIFT : 0, now, delay);
    }
    light(a < 0 ? 0 : a);
    read.textContent = a < 0 ? "rest" : geese[a].name;
    B.wake();
  }

  light(0);
  bag.add(pointer(stage, { move: (p) => setActive(hit(p)), leave: () => setActive(-1) }));
  bag.add(() => svg.replaceChildren());

  return {
    set: (v) => { stag = v; },
    destroy: bag.dispose,
  };
}

hairline({
  name: "skein",
  means: "Five geese fly in a V: the one under the pointer beats its wings, and the beat passes down the line.",
  rules: [1, 2, 5, 10],
  range: [0, 45, 90],
  tour: [[232, 128], [159, 169], [308, 247], null],
  mount,
});
