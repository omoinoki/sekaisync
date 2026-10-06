(function (root) {
  'use strict';
  const durations = Object.freeze([5, 6, 8, 7, 9, 12, 5, 18]);
  const starts = Object.freeze(durations.map((_, i) => durations.slice(0, i).reduce((a, b) => a + b, 0)));
  const total = durations.reduce((a, b) => a + b, 0);
  const targets = [[0, 0, 0, .62], [32, -18, 0, .42], [-26, 14, 0, .26], [22, 20, 0, .3],
    [40, -28, 1, .16], [-30, -10, 0, .16], [18, 24, 0, .38], [0, 0, 0, .58]];
  const clamp = (value, low = 0, high = 1) => Math.max(low, Math.min(high, value));
  const smooth = value => { const t = clamp(value); return t * t * t * (t * (t * 6 - 15) + 10); };
  const ramp = (t, from, to) => smooth((t - from) / (to - from));
  const pulse = (t, from, to) => ramp(t, from, from + .35) * (1 - ramp(t, to, to + .35));
  const mix = (a, b, progress) => a + (b - a) * progress;
  function validTime(seconds) {
    if (typeof seconds !== 'number' || !Number.isFinite(seconds)) throw new TypeError('Time must be a finite number.');
    return clamp(seconds, 0, total);
  }
  function sceneAt(seconds) {
    const t = validTime(seconds);
    let index = 0;
    while (index < starts.length - 1 && t >= starts[index + 1]) index++;
    return index;
  }
  function sceneAlpha(seconds, index, overview = false) {
    if (overview) return sceneAt(seconds) === index ? 1 : 0;
    const incoming = index ? ramp(seconds, starts[index] - .25, starts[index] + .5) : 1;
    const end = starts[index] + durations[index];
    const outgoing = index === starts.length - 1 ? 1 : 1 - ramp(seconds, end - .25, end + .5);
    return incoming * outgoing;
  }
  function worldAt(seconds, overview = false) {
    const t = validTime(seconds);
    if (overview) return [...targets[sceneAt(t)]];
    let result = [...targets[0]];
    for (let i = 1; i < starts.length; i++) {
      const progress = ramp(t, starts[i] - .5, starts[i] + .5);
      if (progress <= 0) break;
      result = targets[i].map((value, axis) => mix(result[axis], value, progress));
    }
    return result;
  }
  function cueAt(localTime, start, overview = false) {
    const opacity = overview ? 1 : ramp(localTime, start, start + .48);
    return { opacity, y: (1 - opacity) * 12 };
  }
  function desktopAt(t, overview = false) {
    if (overview) return { split: .5, scale: 1 };
    const split = t < 5 ? mix(.5, .82, ramp(t, 3.1, 4.8)) :
      t < 7.6 ? mix(.82, .18, ramp(t, 5, 7.3)) : mix(.18, .5, ramp(t, 7.6, 9.2));
    return { split, scale: 1 + .022 * pulse(t, 1.1, 6.8) };
  }
  function frameTime(frame, fps = 30) {
    if (!Number.isInteger(fps) || fps < 1 || fps > 120) throw new RangeError('FPS must be an integer from 1 to 120.');
    if (!Number.isInteger(frame) || frame < 0 || frame >= total * fps) throw new RangeError(`Frame lies outside the ${total}-second timeline.`);
    return frame / fps;
  }
  const TAU = Math.PI * 2;
  const radians = degrees => degrees * Math.PI / 180;
  const seededRandom = initial => {
    let seed = initial;
    return () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; };
  };
  function createFragments(mode = 'drift', width = 1920, height = 1080) {
    if (!['drift', 'ambient'].includes(mode)) throw new RangeError('Unknown fragment mode.');
    const random = seededRandom(2086);
    const range = (a, b) => mix(a, b, random());
    return Array.from({ length: mode === 'drift' ? 24 : 30 }, (_, index) => {
      const shape = mode === 'ambient' || index % 2 === 0 ? 'triangle' : index % 4 === 1 ? 'square' : 'circle';
      const particle = { mode, shape, solid: mode === 'ambient' || index % 6 < 3,
        tone: index % 3, opacity: range(.38, .72), soft: index % 7 === 0,
        size: mode === 'ambient' ? (index % 7 === 0 ? range(60, 100) : range(12, 42)) : range(24, shape === 'triangle' ? 76 : 60),
        x: range(0, width), y: range(0, height), phase: range(0, TAU), sign: index % 2 ? -1 : 1,
        period: range(18, 48), travelX: range(18, 60), travelY: range(18, 60), turn: range(-180, 180),
        speed: range(28, 48), heading: radians(range(28, 42)), omega: radians(range(7, 13)),
        precession: range(24, 48), tilt: range(.55, 2.6), width, height };
      if (mode === 'ambient') {
        particle.x = index < 11 ? range(0, 100) : index < 21 ? range(width - 100, width) : range(160, width - 160);
        particle.y = index < 21 ? range(130, height - 120) : index % 2 ? range(0, 60) : range(height - 60, height);
        return particle;
      }
      // Off-canvas exits and flux-weighted re-entry are scheduled before playback.
      const segments = [];
      let time = 0, x = particle.x, y = particle.y, speed = particle.speed, heading = particle.heading;
      while (time < total) {
        const scale = clamp(width / 1000, .5, 1);
        const vx = Math.cos(heading) * speed * scale, vy = -Math.sin(heading) * speed * scale;
        const end = time + Math.min((width + 120 - x) / vx, (-120 - y) / vy);
        segments.push({ time, end, x, y, vx, vy, speed, heading });
        time = end;
        speed = range(28, 48); heading = radians(range(28, 42));
        const leftFlux = Math.cos(heading) * height, bottomFlux = Math.sin(heading) * width;
        if (random() < leftFlux / (leftFlux + bottomFlux)) { x = -120; y = range(0, height); }
        else { x = range(0, width); y = height + 120; }
      }
      particle.segments = segments;
      const u = random(), a = range(0, TAU), b = range(0, TAU);
      let q = [Math.sqrt(1 - u) * Math.sin(a), Math.sqrt(1 - u) * Math.cos(a), Math.sqrt(u) * Math.sin(b), Math.sqrt(u) * Math.cos(b)];
      const samples = new Float64Array((total * 60 + 1) * 4);
      samples.set(q);
      // Fixed-step quaternion integration follows the handbook's slowly precessing axes.
      for (let step = 1; step <= total * 60; step++) {
        const azimuth = particle.phase + particle.sign * step / 60 * TAU / particle.precession;
        const tilt = particle.tilt + .35 * Math.sin(azimuth * .73 + particle.phase);
        const half = particle.omega * particle.sign / 120, sine = Math.sin(half);
        const dx = Math.sin(tilt) * Math.cos(azimuth) * sine, dy = Math.sin(tilt) * Math.sin(azimuth) * sine;
        const dz = Math.cos(tilt) * sine, dw = Math.cos(half), [qx, qy, qz, qw] = q;
        q = [dw*qx + dx*qw + dy*qz - dz*qy, dw*qy - dx*qz + dy*qw + dz*qx,
          dw*qz + dx*qy - dy*qx + dz*qw, dw*qw - dx*qx - dy*qy - dz*qz];
        const norm = Math.hypot(...q);
        q = q.map(value => value / norm);
        samples.set(q, step * 4);
      }
      particle.orientations = samples;
      return particle;
    });
  }
  function fragmentAt(particle, seconds) {
    const t = validTime(seconds);
    if (particle.mode === 'ambient') {
      const phase = particle.phase + particle.sign * t * TAU / particle.period;
      return { x: particle.x + Math.sin(phase) * particle.travelX / 2,
        y: particle.y + Math.cos(phase) * particle.travelY / 2,
        rotation: particle.turn + Math.sin(phase) * 12 };
    }
    const segment = particle.segments.find(segment => t < segment.end) || particle.segments.at(-1);
    const step = Math.min(total * 60 - 1, Math.floor(t * 60)), fraction = t * 60 - step;
    const q = Array.from({ length: 4 }, (_, axis) => mix(particle.orientations[step * 4 + axis], particle.orientations[(step + 1) * 4 + axis], fraction));
    const norm = Math.hypot(...q);
    return { x: segment.x + segment.vx * (t - segment.time), y: segment.y + segment.vy * (t - segment.time), q: q.map(value => value / norm) };
  }
  root.SekaiVideoTimeline = Object.freeze({ durations, starts, total, clamp, ramp, pulse, mix,
    validTime, sceneAt, sceneAlpha, worldAt, cueAt, desktopAt, frameTime, createFragments, fragmentAt });
})(globalThis);
