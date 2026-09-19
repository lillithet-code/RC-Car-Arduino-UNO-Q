const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../templates/dashboard.html'), 'utf8');
const script = html.split('// Keyboard controls share the joystick command and heartbeat path.')[1].split('</script>')[0];

function setup() {
  const events = {};
  const commands = [];
  const timers = [];
  const pointer = {};
  const context = vm.createContext({
    window: { addEventListener: (name, handler) => { events[name] = handler; } },
    document: { hidden: false, addEventListener: (name, handler) => { events[name] = handler; } },
    hasActiveSession: true,
    dragging: false,
    fullscreenPointerId: null,
    activeJoystick: null,
    videoStage: null,
    fullscreenToggle: null,
    knob: { style: {} },
    joystick: { addEventListener: (name, handler) => { pointer[name] = handler; }, setPointerCapture() {} },
    currentDesiredState: { throttle: 0, steering: 0, stop: true, lights: true },
    commandHeartbeatIntervalMs: 100,
    setInterval: fn => timers.push(fn),
    endJoystickDrag() { context.dragging = false; },
    resetKnob() {},
    isFullscreenActive: () => false,
    updateFullscreenToggleLabel() {},
    renderStreamDiagnostics() {},
    updateFromPointer() { context.sendDesiredState({ throttle: 0.5, steering: 0.2, stop: false }); },
    sendDesiredState(state) {
      context.currentDesiredState = { ...context.currentDesiredState, ...state };
      if (context.hasActiveSession) commands.push({ ...context.currentDesiredState });
    },
  });
  vm.runInContext(script, context);
  function key(type, key, extra = {}) {
    let prevented = false;
    events[type]({ key, target: null, preventDefault() { prevented = true; }, ...extra });
    return prevented;
  }
  return { context, events, commands, timers, pointer, key, last: () => commands.at(-1) };
}

test('WASD supports driving and steering together, then stops on release', () => {
  const t = setup();
  t.key('keydown', 'W'); t.key('keydown', 'a');
  assert.equal(t.last().throttle, 1); assert.equal(t.last().steering, -1);
  t.key('keyup', 'W');
  assert.equal(t.last().throttle, 0); assert.equal(t.last().steering, -1);
  t.timers[0](); // Steering-only input must still reach the board watchdog.
  assert.equal(t.last().steering, -1);
  t.key('keyup', 'a');
  assert.equal(t.last().stop, true); assert.equal(t.last().steering, 0);
  assert.equal(t.last().lights, true);
});

test('arrows prevent scrolling, aliases do not double speed, opposites cancel', () => {
  const t = setup();
  assert.equal(t.key('keydown', 'ArrowUp'), true);
  t.key('keydown', 'w'); t.key('keydown', 'ArrowRight');
  assert.equal(t.last().throttle, 1); assert.equal(t.last().steering, 1);
  t.key('keydown', 's'); t.key('keydown', 'ArrowLeft');
  assert.equal(t.last().throttle, 0); assert.equal(t.last().steering, 0);
  t.key('keyup', 's'); t.key('keyup', 'w');
  assert.equal(t.last().throttle, 1);
  t.key('keyup', 'ArrowUp'); t.key('keydown', 'ArrowDown');
  assert.equal(t.last().throttle, -1);
});

test('blur, hidden page and page exit clear motion and ignore held-key repeats', () => {
  for (const event of ['blur', 'pagehide', 'visibilitychange']) {
    const t = setup();
    t.key('keydown', 'w');
    if (event === 'visibilitychange') t.context.document.hidden = true;
    t.events[event]();
    assert.equal(t.last().stop, true); assert.equal(t.last().throttle, 0);
    const count = t.commands.length;
    t.context.document.hidden = false;
    t.key('keydown', 'w', { repeat: true }); t.timers[0]();
    assert.equal(t.commands.length, count);
  }
});

test('form focus, shortcuts and inactive sessions cannot start driving', () => {
  const t = setup();
  assert.equal(t.key('keydown', 'w', { target: { closest: () => ({}) } }), false);
  t.key('keydown', 'w', { ctrlKey: true });
  t.key('keydown', 'w', { altKey: true });
  t.key('keydown', 'w', { metaKey: true });
  t.context.hasActiveSession = false;
  t.key('keydown', 'w');
  assert.equal(t.commands.length, 0);
  t.context.hasActiveSession = true;
  t.key('keydown', 'w');
  t.events.focusin({ target: { closest: () => ({}) } });
  assert.equal(t.last().throttle, 0);
});

test('joystick takes over from keys without a later keyup stopping it', () => {
  const t = setup();
  t.key('keydown', 'w');
  t.pointer.pointerdown({ pointerId: 1, clientX: 0, clientY: 0 });
  const count = t.commands.length;
  t.key('keyup', 'w');
  assert.equal(t.commands.length, count);
  assert.equal(t.last().throttle, 0.5);
  t.key('keydown', 's');
  assert.equal(t.context.dragging, false);
  assert.equal(t.last().throttle, -1);
});

test('session loss clears held keys and fullscreen exit stops motion', () => {
  const t = setup();
  t.key('keydown', 'w');
  t.context.hasActiveSession = false; t.timers[0]();
  assert.equal(vm.runInContext('heldDriveKeys.size', t.context), 0);
  assert.equal(t.context.currentDesiredState.throttle, 0);
  t.context.hasActiveSession = true;
  const count = t.commands.length;
  t.timers[0]();
  assert.equal(t.commands.length, count);
  t.key('keyup', 'w'); t.key('keydown', 'd');
  t.events.fullscreenchange();
  assert.equal(t.last().stop, true); assert.equal(t.last().steering, 0);
});
