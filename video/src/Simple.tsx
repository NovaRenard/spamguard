import React from 'react';
import {AbsoluteFill, Sequence, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {Appear, Bubble, C, Card, Chip, FONT, MONO, clamp} from './ui';

/**
 * Простое объяснение: ОДИН экран = ОДНА мысль.
 * Крупный текст, мало слов, после появления всего текста экран держится ещё несколько секунд.
 */

const STEPS = ['Сообщение', 'Знакомый?', 'Проверка', 'Баллы', 'Решение'];

// ---- длительности экранов (кадры, 30 fps)
const D = {
  title: 120,
  overview: 360,
  qr: 300,
  storage: 300,
  known: 270,
  brain1: 330,
  brain2: 300,
  profile: 300,
  sum: 330,
  weights: 300,
  decision: 300,
  test: 330,
  limits: 270,
  outro: 120,
};
export const TOTAL_FRAMES = Object.values(D).reduce((a, b) => a + b, 0);

// ---------------------------------------------------------------- общие блоки
const Breadcrumb: React.FC<{stage: number}> = ({stage}) => (
  <div style={{display: 'flex', gap: 14, justifyContent: 'center'}}>
    {STEPS.map((s, i) => {
      const active = i === stage;
      const done = i < stage;
      return (
        <div
          key={s}
          style={{
            padding: '8px 22px',
            borderRadius: 999,
            fontSize: 26,
            fontWeight: 700,
            border: `2px solid ${active ? C.accent : C.line}`,
            background: active ? C.accent : 'transparent',
            color: active ? '#fff' : done ? C.text : C.muted,
          }}
        >
          {i + 1}. {s}
        </div>
      );
    })}
  </div>
);

/** Один экран: сверху «где мы», по центру заголовок, подзаголовок и один визуал. */
const Screen: React.FC<{
  dur: number;
  stage?: number; // 0..4 — активный шаг пути; не задан — показываем tag
  tag?: string;
  title: string;
  sub?: string;
  children?: React.ReactNode;
}> = ({dur, stage, tag, title, sub, children}) => {
  const f = useCurrentFrame();
  const o = interpolate(f, [0, 12, dur - 12, dur], [0, 1, 1, 0], clamp);
  return (
    <AbsoluteFill style={{background: C.bg, fontFamily: FONT, color: C.text, opacity: o}}>
      <div style={{paddingTop: 46, height: 90}}>
        {stage !== undefined ? (
          <Breadcrumb stage={stage} />
        ) : (
          <div style={{textAlign: 'center', fontSize: 28, letterSpacing: 4, color: C.accent, fontWeight: 700}}>{tag}</div>
        )}
      </div>
      <div style={{textAlign: 'center', marginTop: 40, padding: '0 120px'}}>
        <div style={{fontSize: 76, fontWeight: 800, lineHeight: 1.15}}>{title}</div>
        {sub && <div style={{fontSize: 42, color: C.muted, marginTop: 16}}>{sub}</div>}
      </div>
      <div
        style={{
          position: 'absolute',
          left: 120,
          right: 120,
          top: 330,
          bottom: 60,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 34,
        }}
      >
        {children}
      </div>
    </AbsoluteFill>
  );
};

const Line: React.FC<{at: number; children: React.ReactNode; color?: string; size?: number}> = ({
  at,
  children,
  color = C.text,
  size = 40,
}) => (
  <Appear at={at} style={{textAlign: 'center', maxWidth: 1450}}>
    <div style={{fontSize: size, lineHeight: 1.35, color}}>{children}</div>
  </Appear>
);

const Foot: React.FC<{at: number; children: React.ReactNode}> = ({at, children}) => (
  <Appear at={at} style={{textAlign: 'center', maxWidth: 1400}}>
    <div style={{fontSize: 27, color: C.muted, lineHeight: 1.35}}>{children}</div>
  </Appear>
);

const Big: React.FC<{icon: string; label: string; sub?: string; color?: string; w?: number}> = ({
  icon,
  label,
  sub,
  color = C.line,
  w = 300,
}) => (
  <Card border={color} style={{width: w, textAlign: 'center', padding: 24}}>
    <div style={{fontSize: 76}}>{icon}</div>
    <div style={{fontSize: 34, fontWeight: 700, marginTop: 8, lineHeight: 1.2}}>{label}</div>
    {sub && <div style={{fontSize: 26, color: C.muted, marginTop: 8, lineHeight: 1.3}}>{sub}</div>}
  </Card>
);

const Arrow: React.FC<{at: number}> = ({at}) => (
  <Appear at={at} from="none">
    <div style={{fontSize: 60, color: C.accent}}>→</div>
  </Appear>
);

// ---------------------------------------------------------------- 0. Заставка
const Title: React.FC = () => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const s = spring({frame: f, fps, config: {damping: 14}});
  const o = interpolate(f, [0, 12, D.title - 12, D.title], [0, 1, 1, 0], clamp);
  return (
    <AbsoluteFill
      style={{background: C.bg, alignItems: 'center', justifyContent: 'center', fontFamily: FONT, color: C.text, opacity: o}}
    >
      <div style={{fontSize: 190, transform: `scale(${0.6 + 0.4 * s})`}}>🛡</div>
      <div style={{fontSize: 108, fontWeight: 800, marginTop: 10}}>SpamGuard</div>
      <div style={{fontSize: 44, color: C.accent, marginTop: 14}}>Как это работает под капотом</div>
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------- 1. Весь путь
const Overview: React.FC = () => {
  const nodes = [
    {i: '✉️', t: 'Пришло сообщение', s: 'от незнакомого человека'},
    {i: '🤝', t: 'Знакомый или чужой?', s: 'знакомых не трогаем'},
    {i: '🔍', t: 'Проверка', s: 'текст и профиль'},
    {i: '➕', t: 'Считаем баллы', s: 'насколько подозрительно'},
    {i: '🚦', t: 'Решение', s: 'пропустить, предупредить, заблокировать'},
  ];
  return (
    <Screen dur={D.overview} tag="ВЕСЬ ПУТЬ" title="Что бот делает с сообщением" sub="Пять шагов, от письма до решения">
      <div style={{display: 'flex', alignItems: 'center', gap: 14}}>
        {nodes.map((n, k) => (
          <React.Fragment key={n.t}>
            <Appear at={20 + k * 36}>
              <Big icon={n.i} label={n.t} sub={n.s} w={270} />
            </Appear>
            {k < nodes.length - 1 && <Arrow at={34 + k * 36} />}
          </React.Fragment>
        ))}
      </div>
      <Line at={230} color={C.accent}>
        Дальше разберём каждый шаг по отдельности
      </Line>
    </Screen>
  );
};

// ---------------------------------------------------------------- QR: как бот получает доступ
const FakeQR: React.FC<{size: number}> = ({size}) => {
  const N = 25;
  const cell = size / N;
  let seed = 7;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
  const finder = (x: number, y: number) => (x < 8 && y < 8) || (x >= N - 8 && y < 8) || (x < 8 && y >= N - 8);
  const cells: React.ReactNode[] = [];
  for (let y = 0; y < N; y++)
    for (let x = 0; x < N; x++) {
      let on = rnd() > 0.5;
      if (finder(x, y)) {
        const fx = x < 8 ? x : x - (N - 8);
        const fy = y < 8 ? y : y - (N - 8);
        on = fx < 7 && fy < 7 && (fx === 0 || fx === 6 || fy === 0 || fy === 6 || (fx >= 2 && fx <= 4 && fy >= 2 && fy <= 4));
      }
      if (on) cells.push(<rect key={`${x}-${y}`} x={x * cell} y={y * cell} width={cell + 0.5} height={cell + 0.5} fill="#0e1621" />);
    }
  return (
    <svg width={size} height={size} style={{background: '#fff', borderRadius: 12, padding: 10, boxSizing: 'content-box'}}>
      {cells}
    </svg>
  );
};

const Qr: React.FC = () => (
  <Screen dur={D.qr} tag="ПОДГОТОВКА" title="Как бот видит твои сообщения" sub="Ты сам разрешаешь это по QR-коду">
    <div style={{display: 'flex', alignItems: 'center', gap: 50}}>
      <Appear at={14}>
        <Big icon="📱" label="Твой Telegram" w={280} />
      </Appear>
      <Arrow at={34} />
      <Appear at={44}>
        <div style={{textAlign: 'center'}}>
          <FakeQR size={190} />
          <div style={{fontSize: 22, color: C.muted, marginTop: 6}}>учебная картинка</div>
        </div>
      </Appear>
      <Arrow at={64} />
      <Appear at={74}>
        <Big icon="🤖" label="Бот" w={280} />
      </Appear>
    </div>
    <Line at={110}>Бот показывает QR-код, а ты сканируешь его на втором устройстве. Так же входят в Telegram на компьютере.</Line>
    <Foot at={170}>Для этого используется программа Telethon: она умеет заходить в Telegram как обычное приложение.</Foot>
  </Screen>
);

const Storage: React.FC = () => (
  <Screen dur={D.storage} tag="ПОДГОТОВКА" title="Что бот хранит" sub="и чего не хранит">
    <div style={{display: 'flex', gap: 40}}>
      <Appear at={14}>
        <Card border={C.green} style={{width: 700, padding: 34}}>
          <div style={{fontSize: 40, fontWeight: 800, color: C.green}}>Хранит</div>
          <div style={{fontSize: 36, lineHeight: 1.6, marginTop: 8}}>
            🔒 ключ входа, в зашифрованном виде
            <br />
            🤝 список знакомых чатов
            <br />
            📊 цифры для статистики
          </div>
        </Card>
      </Appear>
      <Appear at={60}>
        <Card border={C.red} style={{width: 700, padding: 34}}>
          <div style={{fontSize: 40, fontWeight: 800, color: C.red}}>Не хранит</div>
          <div style={{fontSize: 36, lineHeight: 1.6, marginTop: 8}}>
            ✉️ тексты сообщений
            <br />
            🔑 пароль от Telegram
            <br />
            🖼 картинки и файлы
          </div>
        </Card>
      </Appear>
    </div>
    <Line at={130} color={C.accent}>
      Команда /stop, и доступ удаляется сразу
    </Line>
  </Screen>
);

// ---------------------------------------------------------------- Шаг 2: знакомый или чужой
const Known: React.FC = () => (
  <Screen dur={D.known} stage={1} title="Сначала: знакомый или чужой?" sub="Проверяем только чужих">
    <div style={{display: 'flex', gap: 40, alignItems: 'center'}}>
      <Appear at={14}>
        <Card border={C.green} style={{width: 650, padding: 32, textAlign: 'center'}}>
          <div style={{fontSize: 80}}>🤝</div>
          <div style={{fontSize: 42, fontWeight: 800}}>Знакомый</div>
          <div style={{fontSize: 30, color: C.muted, marginTop: 6, lineHeight: 1.35}}>он в твоих контактах, или ты ему уже писал</div>
          <div style={{fontSize: 36, color: C.green, marginTop: 14, fontWeight: 700}}>пропускаем</div>
        </Card>
      </Appear>
      <Appear at={70}>
        <Card border={C.amber} style={{width: 650, padding: 32, textAlign: 'center'}}>
          <div style={{fontSize: 80}}>👤</div>
          <div style={{fontSize: 42, fontWeight: 800}}>Чужой</div>
          <div style={{fontSize: 30, color: C.muted, marginTop: 6, lineHeight: 1.35}}>незнакомый человек пишет тебе в первый раз</div>
          <div style={{fontSize: 36, color: C.amber, marginTop: 14, fontWeight: 700}}>проверяем</div>
        </Card>
      </Appear>
    </div>
    <Line at={150}>Если ты сам ответил, человек становится знакомым навсегда.</Line>
  </Screen>
);

// ---------------------------------------------------------------- Шаг 3: «мозг»
const Brain1: React.FC = () => (
  <Screen dur={D.brain1} stage={2} title="Как бот понимает, что это спам?" sub="Внутри маленькая обучаемая «нейросеточка»">
    <div style={{display: 'flex', alignItems: 'center', gap: 36}}>
      <Appear at={14}>
        <div style={{width: 560}}>
          <Bubble text="Лучшее казино! 100 фриспинов 🎰" side="in" />
        </div>
      </Appear>
      <Arrow at={44} />
      <Appear at={54}>
        <div style={{fontSize: 130}}>🧠</div>
      </Appear>
      <Arrow at={84} />
      <Appear at={94}>
        <Card border={C.red} style={{padding: '22px 34px', textAlign: 'center'}}>
          <div style={{fontSize: 34}}>Это казино</div>
          <div style={{fontSize: 78, fontWeight: 800, color: C.red}}>94%</div>
        </Card>
      </Appear>
    </div>
    <Line at={140}>
      Это не большая нейросеть вроде ChatGPT, а очень маленькая программа. Она училась на примерах: что пишут спамеры и что пишут обычные люди.
    </Line>
    <Foot at={200}>Для знающих: TF-IDF + логистическая регрессия. 98% на маленькой проверке, на реальных сообщениях будет хуже.</Foot>
  </Screen>
);

const Brain2: React.FC = () => (
  <Screen dur={D.brain2} stage={2} title="Что она запомнила" sub="Слова-подсказки">
    <Appear at={14}>
      <Card border={C.red} style={{width: 1400, padding: 28}}>
        <div style={{fontSize: 34, color: C.red, fontWeight: 800, marginBottom: 6}}>Подозрительные</div>
        {['казино', 'фриспины', 'бонус', 'ставки', 'заработок без вложений', 'вы выиграли'].map((w) => (
          <Chip key={w} text={w} color={C.red} mono={false} />
        ))}
      </Card>
    </Appear>
    <Appear at={60}>
      <Card border={C.green} style={{width: 1400, padding: 28}}>
        <div style={{fontSize: 34, color: C.green, fontWeight: 800, marginBottom: 6}}>Нормальные</div>
        {['привет', 'как дела', 'встретимся', 'спасибо', 'пришли файл', 'завтра в 6'].map((w) => (
          <Chip key={w} text={w} color={C.green} mono={false} />
        ))}
      </Card>
    </Appear>
    <Line at={120}>Много красных слов: подозрительно. Много зелёных: всё в порядке.</Line>
    <Foot at={170}>Хитрости вроде «к а з и н о» или русские буквы вперемешку с английскими бот тоже понимает.</Foot>
  </Screen>
);

const Profile: React.FC = () => (
  <Screen dur={D.profile} stage={2} title="Смотрим и на профиль" sub="Спамера часто видно по «обложке»">
    <div style={{display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24}}>
      {[
        ['🖼', 'Есть ли фото?'],
        ['👥', 'Есть ли общие чаты с тобой?'],
        ['📝', 'Что написано в «О себе»?'],
        ['🏷', 'Не похоже ли имя на «Казино»?'],
      ].map(([i, t], k) => (
        <Appear key={t} at={14 + k * 30}>
          <Card style={{width: 660, padding: '22px 28px', display: 'flex', alignItems: 'center', gap: 22}}>
            <div style={{fontSize: 64}}>{i}</div>
            <div style={{fontSize: 36, lineHeight: 1.25}}>{t}</div>
          </Card>
        </Appear>
      ))}
    </div>
    <Line at={150} color={C.accent}>
      Каждая странность добавляет баллы подозрения
    </Line>
  </Screen>
);

// ---------------------------------------------------------------- Шаг 4: баллы
const Sum: React.FC = () => {
  const f = useCurrentFrame();
  const total = Math.round(interpolate(f, [110, 160], [0, 61], clamp));
  return (
    <Screen dur={D.sum} stage={3} title="Как складываются баллы" sub="Два подозрения вместе сильнее, чем одно">
      <div style={{display: 'flex', alignItems: 'center', gap: 36}}>
        <Appear at={14}>
          <Card style={{padding: '22px 34px', textAlign: 'center'}}>
            <div style={{fontSize: 32, color: C.muted}}>текст</div>
            <div style={{fontSize: 78, fontWeight: 800, color: C.amber}}>40%</div>
          </Card>
        </Appear>
        <Appear at={44} from="none">
          <div style={{fontSize: 64, color: C.accent}}>+</div>
        </Appear>
        <Appear at={64}>
          <Card style={{padding: '22px 34px', textAlign: 'center'}}>
            <div style={{fontSize: 32, color: C.muted}}>профиль</div>
            <div style={{fontSize: 78, fontWeight: 800, color: C.amber}}>35%</div>
          </Card>
        </Appear>
        <Appear at={104} from="none">
          <div style={{fontSize: 64, color: C.accent}}>=</div>
        </Appear>
        <Appear at={108}>
          <Card border={C.amber} style={{padding: '22px 40px', textAlign: 'center'}}>
            <div style={{fontSize: 32, color: C.muted}}>итого</div>
            <div style={{fontSize: 96, fontWeight: 800, color: C.amber}}>{total}%</div>
          </Card>
        </Appear>
      </div>
      <Line at={170}>Не просто 40 + 35: когда сомнения есть и в тексте, и в профиле, балл растёт быстрее.</Line>
      <Foot at={220}>Формула: итог = 1 − (1 − текст) × (1 − профиль)</Foot>
    </Screen>
  );
};

const Weights: React.FC = () => (
  <Screen dur={D.weights} stage={3} title="Что важнее?" sub="Одни признаки громкие, другие тихие">
    <div style={{display: 'flex', gap: 36}}>
      <Appear at={14}>
        <Card border={C.red} style={{width: 700, padding: 30}}>
          <div style={{fontSize: 38, fontWeight: 800, color: C.red}}>Громкие: хватает одного</div>
          <div style={{fontSize: 32, lineHeight: 1.55, marginTop: 10}}>
            🚩 Telegram сам пометил аккаунт как мошеннический
            <br />
            🚩 В «О себе» реклама казино
            <br />
            🚩 Имя как у казино
          </div>
        </Card>
      </Appear>
      <Appear at={70}>
        <Card border={C.amber} style={{width: 700, padding: 30}}>
          <div style={{fontSize: 38, fontWeight: 800, color: C.amber}}>Тихие: только вместе</div>
          <div style={{fontSize: 32, lineHeight: 1.55, marginTop: 10}}>
            🔸 нет фото
            <br />
            🔸 нет общих чатов
            <br />
            🔸 ссылка в сообщении
            <br />
            🔸 много сообщений подряд
          </div>
        </Card>
      </Appear>
    </div>
    <Line at={150}>Тихие признаки вместе дают не больше 35%. Сами по себе они никого не заблокируют: у живых людей тоже бывает без фото.</Line>
  </Screen>
);

// ---------------------------------------------------------------- Шаг 5: решение
const Decision: React.FC = () => (
  <Screen dur={D.decision} stage={4} title="Решение" sub="Что бот делает с итоговым баллом">
    <div style={{display: 'flex', gap: 30}}>
      {[
        {c: C.green, p: 'меньше 50%', t: 'Ничего не делаем', i: '✅', at: 14},
        {c: C.amber, p: '50–85%', t: 'Присылаем тебе предупреждение', i: '⚠️', at: 60},
        {c: C.red, p: '85% и больше', t: 'Блокируем (если включён режим /live)', i: '🛡', at: 106},
      ].map((d) => (
        <Appear key={d.p} at={d.at}>
          <Card border={d.c} style={{width: 470, height: 330, textAlign: 'center', padding: 26}}>
            <div style={{fontSize: 80}}>{d.i}</div>
            <div style={{fontSize: 46, fontWeight: 800, color: d.c, marginTop: 6}}>{d.p}</div>
            <div style={{fontSize: 33, marginTop: 12, lineHeight: 1.3}}>{d.t}</div>
          </Card>
        </Appear>
      ))}
    </div>
    <Line at={170} color={C.accent}>
      Сначала бот только предупреждает. Блокировку включаешь ты сам.
    </Line>
  </Screen>
);

// ---------------------------------------------------------------- Бонус: тестовый режим
const TestMode: React.FC = () => (
  <Screen dur={D.test} tag="БОНУС" title="Как проверить без риска" sub="Режим /testmode">
    <div style={{display: 'flex', flexDirection: 'column', gap: 18, width: 1100}}>
      <Appear at={14}>
        <Bubble text="1win бонус 100 фриспинов t.me/test" side="in" mono />
      </Appear>
      <Appear at={70}>
        <Bubble text="🛡 [тест] я бы заблокировал: продвигает азартные игры (риск 97%)" side="out" />
      </Appear>
    </div>
    <Line at={130}>Друг пишет тебе «спам», а бот никого не блокирует. Он только отвечает, что бы он сделал.</Line>
    <Foot at={190}>Метка «[тест]» нужна, чтобы бот не реагировал на свои же ответы.</Foot>
  </Screen>
);

const Limits: React.FC = () => (
  <Screen dur={D.limits} tag="ЧЕСТНО" title="Бот пока учится" sub="Он может ошибаться">
    <div style={{display: 'flex', flexDirection: 'column', gap: 20, width: 1200}}>
      {[
        ['📚', 'Учился на небольшом наборе примеров'],
        ['🖼', 'Картинки и группы пока не проверяет'],
        ['👍', 'Ошибся? Нажми «Это не спам», и бот запомнит человека'],
      ].map(([i, t], k) => (
        <Appear key={t} at={14 + k * 45}>
          <Card style={{padding: '20px 30px', display: 'flex', alignItems: 'center', gap: 24}}>
            <div style={{fontSize: 56}}>{i}</div>
            <div style={{fontSize: 36, lineHeight: 1.3}}>{t}</div>
          </Card>
        </Appear>
      ))}
    </div>
  </Screen>
);

const Outro: React.FC = () => {
  const f = useCurrentFrame();
  const o = interpolate(f, [0, 14, D.outro - 14, D.outro], [0, 1, 1, 0], clamp);
  return (
    <AbsoluteFill
      style={{background: C.bg, alignItems: 'center', justifyContent: 'center', fontFamily: FONT, color: C.text, opacity: o}}
    >
      <div style={{fontSize: 130}}>🛡</div>
      <div style={{fontSize: 90, fontWeight: 800}}>SpamGuard</div>
      <div style={{fontSize: 42, color: C.muted, marginTop: 14, fontFamily: MONO}}>сначала смотрит, потом защищает</div>
    </AbsoluteFill>
  );
};

export const SimpleExplainer: React.FC = () => {
  let t = 0;
  const seq = (d: number, node: React.ReactNode, key: string) => {
    const el = (
      <Sequence key={key} from={t} durationInFrames={d}>
        {node}
      </Sequence>
    );
    t += d;
    return el;
  };
  return (
    <AbsoluteFill style={{background: C.bg}}>
      {seq(D.title, <Title />, 'title')}
      {seq(D.overview, <Overview />, 'overview')}
      {seq(D.qr, <Qr />, 'qr')}
      {seq(D.storage, <Storage />, 'storage')}
      {seq(D.known, <Known />, 'known')}
      {seq(D.brain1, <Brain1 />, 'brain1')}
      {seq(D.brain2, <Brain2 />, 'brain2')}
      {seq(D.profile, <Profile />, 'profile')}
      {seq(D.sum, <Sum />, 'sum')}
      {seq(D.weights, <Weights />, 'weights')}
      {seq(D.decision, <Decision />, 'decision')}
      {seq(D.test, <TestMode />, 'test')}
      {seq(D.limits, <Limits />, 'limits')}
      {seq(D.outro, <Outro />, 'outro')}
    </AbsoluteFill>
  );
};
