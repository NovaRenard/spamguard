import React from 'react';
import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';

export const C = {
  bg: '#0e1621',
  card: '#17212b',
  card2: '#1e2c3a',
  line: '#2b3b4c',
  accent: '#2aabee',
  red: '#ff5d5d',
  green: '#4cd964',
  amber: '#ffb020',
  text: '#e8eef5',
  muted: '#8b9bb0',
};
export const FONT = '"Segoe UI", Inter, Roboto, Arial, sans-serif';
export const MONO = 'Consolas, "Cascadia Mono", "Courier New", monospace';

export const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;

/** Плавное появление с небольшим смещением. */
export const Appear: React.FC<{
  at: number;
  children: React.ReactNode;
  style?: React.CSSProperties;
  from?: 'bottom' | 'left' | 'none';
}> = ({at, children, style, from = 'bottom'}) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const p = spring({frame: f - at, fps, config: {damping: 200}});
  const o = interpolate(f, [at, at + 12], [0, 1], clamp);
  const tr =
    from === 'bottom' ? `translateY(${(1 - p) * 34}px)` : from === 'left' ? `translateX(${(1 - p) * -50}px)` : 'none';
  return <div style={{opacity: o, transform: tr, ...style}}>{children}</div>;
};

/** Рамка сцены: номер, заголовок, плавный вход/выход. */
export const SceneFrame: React.FC<{
  n: string;
  title: string;
  dur: number;
  children: React.ReactNode;
}> = ({n, title, dur, children}) => {
  const f = useCurrentFrame();
  const o = interpolate(f, [0, 10, dur - 10, dur], [0, 1, 1, 0], clamp);
  return (
    <AbsoluteFill style={{background: C.bg, fontFamily: FONT, color: C.text, opacity: o}}>
      <div style={{padding: '56px 100px 0'}}>
        <div style={{color: C.accent, fontSize: 26, letterSpacing: 3, fontWeight: 700}}>{n}</div>
        <div style={{fontSize: 60, fontWeight: 800, marginTop: 6}}>{title}</div>
      </div>
      <div style={{position: 'absolute', left: 100, right: 100, top: 220, bottom: 150}}>{children}</div>
    </AbsoluteFill>
  );
};

/** Субтитр внизу кадра. */
export const Caption: React.FC<{text: string; at?: number}> = ({text, at = 8}) => {
  const f = useCurrentFrame();
  const o = interpolate(f, [at, at + 14], [0, 1], clamp);
  return (
    <div
      style={{
        position: 'absolute',
        left: 100,
        right: 100,
        bottom: 46,
        textAlign: 'center',
        fontSize: 34,
        lineHeight: 1.3,
        color: C.text,
        opacity: o,
        background: 'rgba(23,33,43,0.92)',
        border: `1px solid ${C.line}`,
        borderRadius: 18,
        padding: '16px 28px',
        fontFamily: FONT,
      }}
    >
      {text}
    </div>
  );
};

export const Card: React.FC<{children: React.ReactNode; style?: React.CSSProperties; border?: string}> = ({
  children,
  style,
  border,
}) => (
  <div
    style={{
      background: C.card,
      border: `2px solid ${border ?? C.line}`,
      borderRadius: 22,
      padding: 28,
      ...style,
    }}
  >
    {children}
  </div>
);

/** Горизонтальная шкала с анимированным заполнением. */
export const Bar: React.FC<{
  label: string;
  value: number; // 0..1
  at: number;
  color: string;
  labelWidth?: number;
  trackWidth?: number;
  text?: string;
  size?: number;
}> = ({label, value, at, color, labelWidth = 260, trackWidth = 560, text, size = 30}) => {
  const f = useCurrentFrame();
  const v = interpolate(f, [at, at + 30], [0, value], {...clamp});
  const o = interpolate(f, [at - 6, at + 6], [0, 1], clamp);
  return (
    <div style={{display: 'flex', alignItems: 'center', gap: 18, opacity: o, margin: '10px 0'}}>
      <div style={{width: labelWidth, fontSize: size, color: C.text}}>{label}</div>
      <div style={{width: trackWidth, height: 26, background: C.card2, borderRadius: 13, overflow: 'hidden'}}>
        <div style={{width: `${v * 100}%`, height: '100%', background: color, borderRadius: 13}} />
      </div>
      <div style={{fontSize: size, fontWeight: 700, color, width: 110}}>{text ?? `${Math.round(v * 100)}%`}</div>
    </div>
  );
};

export const Chip: React.FC<{text: string; color?: string; mono?: boolean}> = ({text, color = C.accent, mono = true}) => (
  <span
    style={{
      display: 'inline-block',
      padding: '6px 16px',
      margin: 5,
      borderRadius: 999,
      border: `2px solid ${color}`,
      color,
      fontSize: 28,
      fontFamily: mono ? MONO : FONT,
      background: 'rgba(42,171,238,0.08)',
    }}
  >
    {text}
  </span>
);

export const Bubble: React.FC<{text: string; side?: 'in' | 'out'; color?: string; mono?: boolean}> = ({
  text,
  side = 'in',
  color,
  mono,
}) => (
  <div
    style={{
      alignSelf: side === 'in' ? 'flex-start' : 'flex-end',
      maxWidth: 640,
      background: color ?? (side === 'in' ? C.card2 : '#2b5278'),
      borderRadius: 22,
      padding: '16px 24px',
      fontSize: 30,
      lineHeight: 1.3,
      fontFamily: mono ? MONO : FONT,
    }}
  >
    {text}
  </div>
);

export const typed = (text: string, f: number, start: number, cps = 0.9) =>
  text.slice(0, Math.max(0, Math.floor((f - start) * cps)));
