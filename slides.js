(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const timeline = window.SekaiVideoTimeline;
  const { durations, starts, total, sceneAt, sceneAlpha, worldAt, cueAt, desktopAt, ramp, pulse, mix } = timeline;
  const slides = [...document.querySelectorAll('.slide')];
  if (slides.length !== durations.length || slides.some((slide, i) => Number(slide.dataset.duration) !== durations[i])) {
    throw new Error('Slide durations do not match the video timeline.');
  }
  const params = new URLSearchParams(location.search);
  const root = document.documentElement;
  const stage = $('#stage');
  const viewport = $('#viewport');
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const systemDark = matchMedia('(prefers-color-scheme: dark)');
  const capture = params.get('capture') === '1';
  const state = { elapsed: 0, index: -1, playing: false, overview: false, ready: false,
    motion: !reducedMotion.matches,
    fragmentTime: 0, fragmentMode: 'drift',
    theme: 'light', lastFrame: performance.now() };
  const twoDigits = value => String(value).padStart(2, '0');
  const safeGet = key => { try { return localStorage.getItem(key); } catch { return null; } };
  const safeSet = (key, value) => { try { localStorage.setItem(key, value); } catch { /* File playback may prohibit storage. */ } };

  slides.forEach((slide, index) => {
    const button = document.createElement('button');
    button.className = 'slide-dot';
    button.textContent = twoDigits(index + 1);
    button.title = `${slide.dataset.title} · ${durations[index]} 秒`;
    button.setAttribute('aria-label', `第 ${index + 1} 页：${slide.dataset.title}`);
    button.addEventListener('click', () => navigate(index));
    $('#slide-navigation').append(button);
  });
  const dots = [...document.querySelectorAll('.slide-dot')];
  const cues = slides.map(slide => [...slide.querySelectorAll('[data-cue]')]);
  const stationaryCues = new WeakSet(cues.flat().filter(element => element.hasAttribute('data-cue-still') ||
    element.parentElement.closest('[data-cue], .desktop-features, .region-line')));

  function fitStage() {
    const style = getComputedStyle(viewport);
    const width = viewport.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const height = viewport.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
    root.style.setProperty('--stage-scale', String(Math.max(.01, Math.min(width / 1920, height / 1080))));
  }
  new ResizeObserver(fitStage).observe(viewport);
  window.addEventListener('resize', fitStage);
  function applyTheme(theme) {
    state.theme = ['light', 'dark', 'system'].includes(theme) ? theme : 'light';
    root.dataset.theme = state.theme;
    root.dataset.resolvedTheme = state.theme === 'system' ? (systemDark.matches ? 'dark' : 'light') : state.theme;
    $('#theme-select').value = state.theme;
    safeSet('sekaisync-video60-theme', state.theme);
  }
  $('#theme-select').addEventListener('change', event => applyTheme(event.target.value));
  systemDark.addEventListener('change', () => { if (state.theme === 'system') applyTheme('system'); });
  applyTheme(params.get('theme') || safeGet('sekaisync-video60-theme') || 'light');

  const colors = ['var(--sk-fragment-cyan)', 'var(--sk-fragment-pink)', 'var(--sk-fragment-lemon)'];
  let fragments;
  function makeFragments() {
    const field = $('#fragment-field');
    field.replaceChildren(); field.dataset.mode = 'drift';
    fragments = timeline.createFragments('drift').map(particle => {
      const element = document.createElement('div');
      element.className = 'fragment';
      element.dataset.shape = particle.shape;
      element.dataset.fill = particle.solid ? 'solid' : 'outline';
      Object.assign(element.style, { left: '0', top: '0', width: `${particle.size}px`, height: `${particle.size}px`, opacity: String(particle.opacity), color: colors[particle.tone] });
      if (particle.soft) element.classList.add('fragment-soft');
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('viewBox', '0 0 100 100');
      const shape = document.createElementNS('http://www.w3.org/2000/svg', particle.shape === 'triangle' ? 'polygon' : particle.shape === 'square' ? 'rect' : 'circle');
      const attributes = particle.shape === 'triangle' ? { points: '50,8 91,83, 9,83' } :
        particle.shape === 'square' ? { x: 17, y: 17, width: 66, height: 66 } : { cx: 50, cy: 50, r: 34 };
      Object.entries(attributes).forEach(([key, value]) => shape.setAttribute(key, value));
      shape.setAttribute('fill', particle.solid ? 'currentColor' : 'none');
      shape.setAttribute('stroke', particle.solid ? 'none' : 'currentColor');
      shape.setAttribute('stroke-width', '1.7');
      shape.setAttribute('vector-effect', 'non-scaling-stroke');
      svg.append(shape); element.append(svg); field.append(element);
      return { ...particle, element, plane: svg };
    });
  }
  makeFragments();

  function selectScene(index, announce = true) {
    if (state.index === index) return;
    if (slides.some((slide, i) => i !== index && slide.contains(document.activeElement))) $('#play-button').focus({ preventScroll: true });
    state.index = index;
    slides.forEach((slide, i) => {
      slide.classList.toggle('is-active', i === index);
      slide.setAttribute('aria-hidden', String(i !== index));
      slide.inert = i !== index;
      if (i === index) dots[i].setAttribute('aria-current', 'step');
      else dots[i].removeAttribute('aria-current');
    });
    if (announce) $('#status').textContent = `第 ${index + 1} 页，共 8 页：${slides[index].dataset.title}`;
    if (!capture) {
      try { history.replaceState(null, '', `${location.pathname}${location.search}#slide-${index + 1}`); } catch { /* Navigation is independent of history access. */ }
    }
  }
  function updateControls() {
    const label = state.playing ? '暂停播放' : '继续播放';
    $('#play-label').textContent = label;
    $('#play-button').setAttribute('aria-label', `${label}（空格）`);
    $('#play-button').disabled = !state.ready;
    $('#play-icon').setAttribute('href', state.playing ? '#i-pause' : '#i-play');
    $('#fullscreen-play').textContent = label;
    $('#fullscreen-play').disabled = !state.ready;
    stage.dataset.playing = String(state.playing);
    root.dataset.motion = state.motion ? 'running' : 'paused';
    updateBarsVisibility();
  }
  const smallLandscape = matchMedia('(max-height: 500px) and (max-width: 1100px) and (orientation: landscape)');
  let barsTimer;
  function updateBarsVisibility() {
    clearTimeout(barsTimer);
    if (smallLandscape.matches && state.playing) {
      document.body.classList.add('bars-collapsed');
    } else {
      document.body.classList.remove('bars-collapsed');
    }
  }
  function peekBars() {
    document.body.classList.remove('bars-collapsed');
    clearTimeout(barsTimer);
    barsTimer = setTimeout(updateBarsVisibility, 3000);
  }
  smallLandscape.addEventListener('change', updateBarsVisibility);
  viewport.addEventListener('click', () => { if (smallLandscape.matches && state.playing) peekBars(); });
  viewport.addEventListener('touchstart', () => { if (smallLandscape.matches && state.playing) peekBars(); }, { passive: true });
  function render() {
    const t = state.elapsed;
    const overview = state.overview || !state.motion;
    const index = sceneAt(t);
    selectScene(index);
    const loopFade = 0.75;
    const loopStart = total - loopFade;
    const looping = !overview && t >= loopStart;
    const loopProgress = looping ? ramp(t, loopStart, total) : 0;
    slides.forEach((slide, i) => {
      let alpha;
      let localTime;
      if (looping && i === 7) {
        alpha = 1 - loopProgress;
        localTime = t - starts[7];
      } else if (looping && i === 0) {
        alpha = loopProgress;
        localTime = 2;
      } else {
        alpha = sceneAlpha(t, i, overview);
        localTime = t - starts[i];
      }
      slide.classList.toggle('is-exiting', i !== index && alpha > .0001);
      slide.style.opacity = alpha.toFixed(5);
      if (!alpha) return;
      for (const element of cues[i]) {
        const pose = cueAt(localTime, Number(element.dataset.cue), overview);
        element.style.opacity = pose.opacity.toFixed(5);
        element.style.transform = `translate3d(0, ${stationaryCues.has(element) ? 0 : pose.y.toFixed(4)}px, 0)`;
        element.inert = pose.opacity < .05;
      }
    });
    let world;
    if (looping) {
      const worldEnd = worldAt(total - 0.01, overview);
      const worldStart = worldAt(0, overview);
      world = worldEnd.map((value, axis) => mix(value, worldStart[axis], loopProgress));
    } else {
      world = worldAt(t, overview);
    }
    stage.style.setProperty('--world-x', world[0].toFixed(4) + 'px');
    stage.style.setProperty('--world-y', world[1].toFixed(4) + 'px');
    stage.style.setProperty('--night-weight', world[2].toFixed(5));
    stage.style.setProperty('--stage-brand-ink', world[2] > .55 ? '#EDF4FF' : 'var(--sk-ink)');
    stage.style.setProperty('--fragment-density', world[3].toFixed(5));
    stage.style.setProperty('--ambient-y', (overview ? 0 : Math.sin(t * Math.PI * 2 / 38) * 10).toFixed(4) + 'px');
    for (const fragment of fragments) {
      const pose = timeline.fragmentAt(fragment, state.fragmentTime);
      fragment.element.style.transform = `translate3d(${(pose.x - fragment.size / 2).toFixed(4)}px, ${(pose.y - fragment.size / 2).toFixed(4)}px, 0)`;
      if (pose.q) {
        const [x, y, z, w] = pose.q, norm = Math.hypot(x, y, z);
        fragment.plane.style.transform = norm < 1e-8 ? 'none' : `rotate3d(${x/norm},${y/norm},${z/norm},${2*Math.atan2(norm,w)}rad)`;
      } else fragment.plane.style.transform = `rotate(${pose.rotation.toFixed(4)}deg)`;
    }
    const local = t - starts[index];
    $('#slide-2').querySelectorAll('.question-row').forEach((row, i) => row.style.setProperty('--row-focus', overview ? 0 : pulse(t - starts[1], .5 + i * .8, 1.25 + i * .8)));
    $('#slide-3').style.setProperty('--sync-progress', overview ? 1 : ramp(t - starts[2], .8, 3.8));
    $('#slide-4').style.setProperty('--query-focus', overview ? 0 : .16 * pulse(t - starts[3], 1, 2));
    $('#slide-4').style.setProperty('--language-focus', overview ? 0 : .16 * pulse(t - starts[3], 1.8, 2.8));
    $('#slide-5').style.setProperty('--mcp-progress', overview ? 1 : ramp(t - starts[4], 1.8, 2.8));
    $('#slide-5').style.setProperty('--mcp-visible', overview ? 0 : pulse(t - starts[4], 1.7, 2.8));
    const desktop = desktopAt(t - starts[5], overview);
    const windowImage = $('.window-image-stack');
    windowImage.style.setProperty('--theme-split', desktop.split);
    windowImage.style.setProperty('--window-scale', desktop.scale);
    stage.dataset.videoTime = t.toFixed(6);
    stage.dataset.sceneTime = Math.max(0, local).toFixed(6);
    stage.dataset.scene = index === 4 ? 'night' : 'day';
    $('#timeline').setAttribute('aria-valuenow', String(Math.floor(t)));
    $('#timeline-fill').style.width = `${t / total * 100}%`;
  }
  function seek(seconds) {
    state.elapsed = timeline.validTime(seconds);
    state.fragmentTime = state.elapsed;
    state.lastFrame = performance.now();
    render();
    return snapshot();
  }
  function navigate(index) {
    if (index >= 0 && index < slides.length) {
      state.elapsed = timeline.validTime(starts[index]);
      state.fragmentTime = state.elapsed;
      state.lastFrame = performance.now();
      render();
    }
  }
  function togglePlayback() {
    if (!state.ready || capture) return;
    state.overview = false;
    state.playing = !state.playing;
    state.lastFrame = performance.now();
    render(); updateControls();
  }
  function snapshot() {
    return { time: state.elapsed, slide: state.index + 1, duration: total, playing: state.playing,
      overview: state.overview, ready: state.ready, motion: state.motion,
      fragmentTime: state.fragmentTime, fragmentMode: state.fragmentMode,
      world: worldAt(state.elapsed, state.overview || !state.motion),
      desktop: desktopAt(state.elapsed - starts[5], state.overview || !state.motion) };
  }
  $('#play-button').addEventListener('click', togglePlayback);
  $('#fullscreen-play').addEventListener('click', togglePlayback);
  reducedMotion.addEventListener('change', () => {
    if (reducedMotion.matches) { state.motion = false; state.playing = false; render(); updateControls(); }
  });
  async function toggleFullscreen() {
    try { if (document.fullscreenElement) await document.exitFullscreen(); else await viewport.requestFullscreen(); }
    catch { $('#status').textContent = '浏览器未允许全屏，可使用 ?present=1 的纯演示视图。'; }
  }
  $('#fullscreen-button').addEventListener('click', toggleFullscreen);
  $('#exit-fullscreen').addEventListener('click', toggleFullscreen);
  document.addEventListener('fullscreenchange', fitStage);
  let controlsTimer;
  viewport.addEventListener('pointermove', event => {
    if (!document.fullscreenElement || event.clientY < innerHeight - 150) return;
    viewport.classList.add('controls-visible'); clearTimeout(controlsTimer);
    controlsTimer = setTimeout(() => viewport.classList.remove('controls-visible'), 1800);
  });
  document.addEventListener('keydown', event => {
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(event.target.tagName) || event.target.isContentEditable || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.code === 'Space' && /^(BUTTON|A)$/.test(event.target.tagName)) return;
    switch (event.key) {
      case 'ArrowRight': case 'PageDown': event.preventDefault(); navigate(state.index + 1); break;
      case 'ArrowLeft': case 'PageUp': event.preventDefault(); navigate(state.index - 1); break;
      case 'Home': event.preventDefault(); navigate(0); break;
      case 'End': event.preventDefault(); navigate(7); break;
      case ' ': event.preventDefault(); togglePlayback(); break;
      case 'f': case 'F': event.preventDefault(); toggleFullscreen(); break;
      case 'Escape': if (!document.fullscreenElement && !capture) { document.body.classList.remove('presentation-mode'); fitStage(); } break;
    }
  });
  window.addEventListener('hashchange', () => { const match = location.hash.match(/^#slide-([1-8])$/); if (match) navigate(Number(match[1]) - 1); });
  document.addEventListener('visibilitychange', () => { state.lastFrame = performance.now(); });
  function tick(now) {
    const delta = Math.max(0, (now - state.lastFrame) / 1000);
    state.lastFrame = now;
    if (state.playing && state.ready && !document.hidden) {
      const advance = Math.min(total - state.elapsed, delta);
      state.elapsed += advance;
      if (state.motion) state.fragmentTime = Math.min(total, state.fragmentTime + advance);
      if (state.elapsed >= total) {
        state.elapsed = 0;
        state.fragmentTime = 0;
      }
      render();
    }
    requestAnimationFrame(tick);
  }

  if (params.get('present') === '1' || capture) document.body.classList.add('presentation-mode');
  const hash = location.hash.match(/^#slide-([1-8])$/);
  state.elapsed = params.has('t') ? timeline.validTime(Number(params.get('t'))) : starts[hash ? Number(hash[1]) - 1 : 0];
  state.fragmentTime = state.elapsed;
  render(); updateControls(); fitStage();
  const ready = Promise.all([
    document.fonts.ready,
    ...[...document.images].map(image => image.decode().catch(() => {
      throw new Error('Cannot decode presentation image: ' + image.getAttribute('src'));
    }))
  ]).then(() => {
    state.ready = true; root.dataset.videoReady = 'true'; updateControls();
    if (!capture && !reducedMotion.matches) togglePlayback();
    return snapshot();
  });
  ready.catch(error => { $('#status').textContent = '资源加载失败，请检查字体与图片文件。'; console.error(error); });
  window.SekaiVideo = Object.freeze({ ready, seek, snapshot,
    captureFrame: (frame, fps = 30) => seek(timeline.frameTime(frame, fps)),
    play: () => { if (!state.playing) togglePlayback(); },
    pause: () => { state.playing = false; state.lastFrame = performance.now(); updateControls(); },
    duration: total });
  if (!capture) requestAnimationFrame(tick);
})();
