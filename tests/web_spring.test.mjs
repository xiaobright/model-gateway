import assert from 'node:assert/strict';
import test from 'node:test';

import { MOTION, createSpringValue, springEasing, settleTime } from '../web/spring.js';

const frame = 1 / 60;

test('重定向保留当前位置和速度，不从零重新缓动', () => {
  const spring = createSpringValue({ value: 0, ...MOTION.nav });
  spring.set(120);
  for (let i = 0; i < 5; i++) spring.step(frame);
  const at = spring.value;
  const speed = spring.velocity;
  assert.ok(at > 0 && at < 120, `应先走了一段，实际 ${at}`);
  assert.ok(speed > 0, `应带着正向速度，实际 ${speed}`);

  spring.set(0);
  assert.equal(spring.value, at, '改目标不能把位置清掉');
  assert.equal(spring.velocity, speed, '改目标不能把速度清掉');

  spring.step(frame);
  assert.ok(spring.velocity < speed, '反向目标应先把速度刹下来');
});

test('连续推进后停在目标上，速度归零', () => {
  const spring = createSpringValue({ value: 0, ...MOTION.view });
  spring.set(40);
  let rested = false;
  for (let i = 0; i < 240 && !rested; i++) rested = spring.step(frame);
  assert.equal(rested, true);
  assert.equal(spring.value, 40);
  assert.equal(spring.velocity, 0);
});

test('中途反向能在有限帧内回到原点', () => {
  const spring = createSpringValue({ value: 0, ...MOTION.nav });
  spring.set(100);
  for (let i = 0; i < 8; i++) spring.step(frame);
  spring.set(0);
  let rested = false;
  for (let i = 0; i < 300 && !rested; i++) rested = spring.step(frame);
  assert.equal(rested, true);
  assert.equal(spring.value, 0);
});

test('曲线采样以 0 开始、100% 收在 1，并保留回弹', () => {
  const { easing, duration } = springEasing({ stiffness: 620, damping: 32, mass: 1 }, { samples: 20 });
  assert.match(easing, /^linear\(0, /);
  assert.match(easing, /1 100%\)$/);
  assert.ok(duration >= 120);
  const numbers = [...easing.matchAll(/(-?\d+(?:\.\d+)?) \d+(?:\.\d+)?%/g)].map((m) => Number(m[1]));
  assert.ok(numbers.some((value) => value > 1), '欠阻尼弹簧应有超过 1 的过冲采样');
  assert.equal(numbers.at(-1), 1);
});

test('落位时间随刚度增加而缩短', () => {
  const soft = settleTime({ stiffness: 300, damping: 34, mass: 1 });
  const stiff = settleTime({ stiffness: 900, damping: 44, mass: 1 });
  assert.ok(stiff < soft, `${stiff} 应小于 ${soft}`);
  assert.ok(soft < 1.2 && stiff > 0.2);
});
